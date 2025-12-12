from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os, re, json, sqlite3
import datetime as dt
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
TZ = ZoneInfo("Europe/Rome")

# =========================
# CONFIG
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY mancante nelle variabili d'ambiente.")
ai_client = OpenAI(api_key=OPENAI_API_KEY)

DB_PATH = os.getenv("DB_PATH", "bot.db")

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

WORK_DAYS = {0, 1, 2, 3, 4}  # Lun-Ven
WORK_START_HOUR = int(os.getenv("WORK_START_HOUR", "8"))
WORK_END_HOUR = int(os.getenv("WORK_END_HOUR", "18"))

CONFIRM_WORDS = {"ok", "va bene", "confermo", "confermiamo", "sì", "si", "perfetto", "vai", "conferma"}

# =========================
# PRICES (€/h)
# =========================
SERVICES = {
    "taglio prato": 25,
    "potatura siepi": 30,
    "potatura alberi": 35,      # include ulivo
    "potatura su corda": 60,
    "trattamenti antiparassitari": 40,
    "pulizia giardino": 28,
    "raccolta foglie": 25,
    "smaltimento verde": 30,
}

# =========================
# DB (session + memoria lunga)
# =========================
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        phone TEXT PRIMARY KEY,
        updated_at TEXT NOT NULL,
        state_json TEXT NOT NULL
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT NOT NULL,
        created_at TEXT NOT NULL,
        jobs_json TEXT NOT NULL,
        total_hours REAL,
        total_quote_eur REAL,
        calendar_event_id TEXT,
        start_iso TEXT,
        end_iso TEXT
    )""")
    conn.commit()
    conn.close()

init_db()

def load_session(phone: str) -> dict:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT state_json FROM sessions WHERE phone = ?", (phone,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {
            "phase": "IDLE",                 # IDLE | COLLECTING | QUOTED | BOOK_PREF | BOOK_CONFIRM
            "jobs": [],                      # list jobs
            "pending": None,                 # {"job_i":int, "field":str}
            "proposed_slot": None,           # {"start":iso,"end":iso}
            "short_history": []              # last N turns
        }
    try:
        return json.loads(row["state_json"])
    except Exception:
        return {
            "phase": "IDLE",
            "jobs": [],
            "pending": None,
            "proposed_slot": None,
            "short_history": []
        }

def save_session(phone: str, state: dict):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sessions (phone, updated_at, state_json)
        VALUES (?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            updated_at = excluded.updated_at,
            state_json = excluded.state_json
    """, (phone, dt.datetime.now(TZ).isoformat(), json.dumps(state, ensure_ascii=False)))
    conn.commit()
    conn.close()

def save_history(phone: str, jobs: list, total_hours: float, total_quote: float, event_id: str|None, start_iso: str|None, end_iso: str|None):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO jobs_history (phone, created_at, jobs_json, total_hours, total_quote_eur, calendar_event_id, start_iso, end_iso)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        phone,
        dt.datetime.now(TZ).isoformat(),
        json.dumps(jobs, ensure_ascii=False),
        float(total_hours),
        float(total_quote),
        event_id,
        start_iso,
        end_iso
    ))
    conn.commit()
    conn.close()

def last_history_summary(phone: str, limit: int = 2) -> str:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT jobs_json, total_quote_eur, start_iso
        FROM jobs_history
        WHERE phone = ?
        ORDER BY id DESC
        LIMIT ?
    """, (phone, limit))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return ""
    out = []
    for r in rows:
        when = ""
        if r["start_iso"]:
            try:
                d = dt.datetime.fromisoformat(r["start_iso"]).astimezone(TZ)
                when = d.strftime("%d/%m/%Y %H:%M")
            except Exception:
                when = r["start_iso"]
        out.append(f"- {r['jobs_json']} • {round(r['total_quote_eur'],2)}€" + (f" • {when}" if when else ""))
    return "Storico:\n" + "\n".join(out)

# =========================
# GOOGLE CALENDAR
# =========================
calendar_service = None
try:
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )
    calendar_service = build("calendar", "v3", credentials=credentials)
except Exception:
    calendar_service = None

def freebusy(time_min: dt.datetime, time_max: dt.datetime):
    if not calendar_service:
        return []
    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "timeZone": "Europe/Rome",
        "items": [{"id": GOOGLE_CALENDAR_ID}],
    }
    fb = calendar_service.freebusy().query(body=body).execute()
    busy = fb.get("calendars", {}).get(GOOGLE_CALENDAR_ID, {}).get("busy", [])
    intervals = []
    for b in busy:
        s = dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(TZ)
        e = dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(TZ)
        intervals.append((s, e))
    intervals.sort(key=lambda x: x[0])
    return intervals

def next_work_windows(days_ahead=14):
    now = dt.datetime.now(TZ)
    windows = []
    for i in range(days_ahead):
        day = (now + dt.timedelta(days=i)).date()
        if day.weekday() not in WORK_DAYS:
            continue
        start = dt.datetime(day.year, day.month, day.day, WORK_START_HOUR, 0, tzinfo=TZ)
        end = dt.datetime(day.year, day.month, day.day, WORK_END_HOUR, 0, tzinfo=TZ)
        if i == 0 and now > start:
            start = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=15)
        if start < end:
            windows.append((start, end))
    return windows

def find_slot(duration_hours: float, day_pref: str|None = None, part_pref: str|None = None, start_at: dt.time|None = None):
    """
    Cerca uno slot CONTIGUO di durata_hours.
    day_pref: ISO date 'YYYY-MM-DD' oppure None
    part_pref: 'mattina'|'pomeriggio'|None
    start_at: orario di inizio minimo (es. 14:00) oppure None
    """
    duration = dt.timedelta(hours=float(duration_hours))
    windows = next_work_windows()

    if day_pref:
        try:
            d = dt.date.fromisoformat(day_pref)
            windows = [(w0, w1) for (w0, w1) in windows if w0.date() == d]
        except Exception:
            pass

    def slice_part(w0, w1):
        if part_pref == "mattina":
            a = dt.datetime(w0.year, w0.month, w0.day, WORK_START_HOUR, 0, tzinfo=TZ)
            b = dt.datetime(w0.year, w0.month, w0.day, 13, 0, tzinfo=TZ)
            return max(w0, a), min(w1, b)
        if part_pref == "pomeriggio":
            a = dt.datetime(w0.year, w0.month, w0.day, 14, 0, tzinfo=TZ)
            b = dt.datetime(w0.year, w0.month, w0.day, WORK_END_HOUR, 0, tzinfo=TZ)
            return max(w0, a), min(w1, b)
        return w0, w1

    if not windows:
        return None

    overall_min = windows[0][0]
    overall_max = windows[-1][1]
    busy = freebusy(overall_min, overall_max)

    for w0, w1 in windows:
        w0, w1 = slice_part(w0, w1)
        if w0 >= w1:
            continue

        if start_at:
            candidate = dt.datetime(w0.year, w0.month, w0.day, start_at.hour, start_at.minute, tzinfo=TZ)
            if candidate > w0:
                w0 = candidate
            if w0 >= w1:
                continue

        cursor = w0
        for b_start, b_end in busy:
            if b_end <= cursor:
                continue
            if b_start >= w1:
                break
            free_end = min(b_start, w1)
            if free_end - cursor >= duration:
                return (cursor, cursor + duration)
            cursor = max(cursor, b_end)
            if cursor >= w1:
                break

        if w1 - cursor >= duration:
            return (cursor, cursor + duration)

    return None

def create_event(summary, description, start_dt, end_dt):
    if not calendar_service:
        return None
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Rome"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Rome"},
    }
    created = calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created.get("id")

# =========================
# CHECKLIST (dati obbligatori per preventivo serio)
# =========================
def required_fields(service: str):
    if service == "potatura siepi":
        return ["length_m", "height_m", "access", "waste", "obstacles", "parking"]
    if service == "potatura alberi":
        return ["count", "size", "access", "waste", "obstacles", "parking"]
    if service == "potatura su corda":
        return ["count", "height_m", "access", "waste", "obstacles", "parking"]
    if service == "taglio prato":
        return ["area_m2", "access", "slope", "edges"]
    if service == "pulizia giardino":
        return ["area_m2", "waste", "access", "notes"]
    if service == "raccolta foglie":
        return ["area_m2", "waste", "notes"]
    if service == "smaltimento verde":
        return ["volume", "access"]
    if service == "trattamenti antiparassitari":
        return ["plant", "problem", "area_m2"]
    return ["notes"]

def is_missing(job: dict, field: str):
    v = job.get(field)
    if field in {"length_m", "area_m2", "count"}:
        return not isinstance(v, int) or v <= 0
    if field == "height_m":
        return not isinstance(v, (int, float)) or v <= 0
    if field in {"access"}:
        return v not in {"facile", "medio", "difficile"}
    if field in {"waste"}:
        return v not in {"si", "no"}
    if field in {"obstacles", "parking", "slope", "edges"}:
        return v is None
    if field in {"size"}:
        return v not in {"piccolo", "medio", "grande"}
    if field in {"volume"}:
        return not v or len(str(v).strip()) < 2
    if field in {"plant", "problem"}:
        return not v or len(str(v).strip()) < 2
    if field in {"notes"}:
        return not v or len(str(v).strip()) < 3
    return v is None

def question_for(service: str, field: str):
    # Siepi
    if service == "potatura siepi":
        if field == "length_m": return "Quanti metri è lunga la siepe circa?"
        if field == "height_m": return "Altezza media della siepe? (es. 1,5m / 2m / 3m)"
        if field == "access": return "Accesso com’è? (facile / medio / difficile)"
        if field == "waste": return "Vuoi anche lo smaltimento del verde? (sì/no)"
        if field == "obstacles": return "Ci sono ostacoli (muretti, cancelli stretti, scale, aiuole delicate)? (sì/no)"
        if field == "parking": return "Si riesce a parcheggiare vicino per caricare il verde? (sì/no)"
    # Alberi/ulivi
    if service == "potatura alberi":
        if field == "count": return "Quanti alberi/ulivi sono da potare?"
        if field == "size": return "Dimensione alberi? (piccolo / medio / grande)"
        if field == "access": return "Accesso com’è? (facile / medio / difficile)"
        if field == "waste": return "Vuoi anche lo smaltimento del verde? (sì/no)"
        if field == "obstacles": return "Ci sono ostacoli (linee, tettoie, aiuole, spazi stretti)? (sì/no)"
        if field == "parking": return "Si riesce a parcheggiare vicino? (sì/no)"
    # Corda
    if service == "potatura su corda":
        if field == "count": return "Quanti alberi da fare in corda?"
        if field == "height_m": return "Altezza indicativa degli alberi? (metri)"
        if field == "access": return "Accesso com’è? (facile / medio / difficile)"
        if field == "waste": return "Vuoi anche lo smaltimento del verde? (sì/no)"
        if field == "obstacles": return "Ci sono ostacoli (cavi, tetti, spazi stretti)? (sì/no)"
        if field == "parking": return "Si riesce a parcheggiare vicino? (sì/no)"
    # Prato
    if service == "taglio prato":
        if field == "area_m2": return "Quanti mq circa di prato? (es. 200)"
        if field == "access": return "Accesso com’è? (facile / medio / difficile)"
        if field == "slope": return "Il terreno è in pendenza? (sì/no)"
        if field == "edges": return "Serve anche rifinitura bordi/contorni? (sì/no)"
    # Pulizia
    if service == "pulizia giardino":
        if field == "area_m2": return "Quanto è grande l’area da pulire? (mq circa)"
        if field == "waste": return "Vuoi anche lo smaltimento del verde? (sì/no)"
        if field == "access": return "Accesso com’è? (facile / medio / difficile)"
        if field == "notes": return "Che tipo di pulizia serve? (erbacce, rami, rovi, generale…)"
    # Foglie
    if service == "raccolta foglie":
        if field == "area_m2": return "Su che area circa? (mq)"
        if field == "waste": return "Vuoi anche lo smaltimento del verde? (sì/no)"
        if field == "notes": return "Le foglie sono leggere o molto bagnate/compattate? (dimmi due righe)"
    # Smaltimento
    if service == "smaltimento verde":
        if field == "volume": return "Quanto verde da smaltire circa? (es. 10 sacchi / 1 m³ / rimorchio pieno)"
        if field == "access": return "Accesso com’è? (facile / medio / difficile)"
    # Trattamenti
    if service == "trattamenti antiparassitari":
        if field == "plant": return "Che pianta è? (es. rose, ulivo, siepe…)"
        if field == "problem": return "Che problema vedi? (afidi, cocciniglia, macchie, ingiallimento…)"
        if field == "area_m2": return "Quanta superficie/numero piante circa?"
    return "Mi dai un dettaglio in più (misure, accesso, smaltimento, note)?"

def next_missing(job: dict):
    s = job.get("service")
    for f in required_fields(s):
        if is_missing(job, f):
            return f
    return None

# =========================
# STIMA ORE + QUOTE (deterministico, prudente)
# =========================
def access_factor(access: str):
    if access == "medio": return 1.15
    if access == "difficile": return 1.35
    return 1.0

def bool_extra(val: str, yes_add: float):
    return yes_add if val == "si" else 0.0

def estimate_hours(job: dict):
    s = job["service"]
    rate = SERVICES.get(s, 30)

    af = access_factor(job.get("access", "facile"))

    # “extra” tempo per condizioni
    waste_extra = bool_extra(job.get("waste"), 0.8)
    obstacles_extra = 0.4 if job.get("obstacles") == "si" else 0.0
    parking_extra = 0.3 if job.get("parking") == "no" else 0.0

    if s == "potatura siepi":
        length_m = job.get("length_m", 0)
        height_m = job.get("height_m", 0.0)
        # velocità prudente m/h
        if height_m <= 1.5: speed = 55
        elif height_m <= 2.5: speed = 38
        else: speed = 28
        base = max(2.0, round(length_m / speed, 1))
        return round(base * af + waste_extra + obstacles_extra + parking_extra, 1), rate

    if s == "potatura alberi":
        count = job.get("count", 0)
        size = job.get("size", "medio")
        per_tree = 1.8 if size == "piccolo" else (2.6 if size == "medio" else 3.6)
        base = max(1.5, round(count * per_tree, 1))
        return round(base * af + waste_extra + obstacles_extra + parking_extra, 1), rate

    if s == "potatura su corda":
        count = job.get("count", 0)
        height_m = job.get("height_m", 0.0)
        per_tree = 3.5 if height_m <= 12 else 4.8
        base = max(3.0, round(count * per_tree, 1))
        return round(base * af + waste_extra + obstacles_extra + parking_extra, 1), rate

    if s == "taglio prato":
        area = job.get("area_m2", 0)
        slope_extra = 0.5 if job.get("slope") == "si" else 0.0
        edges_extra = 0.4 if job.get("edges") == "si" else 0.0
        base = max(1.0, round(area / 350, 1))
        return round(base * af + slope_extra + edges_extra, 1), rate

    if s == "pulizia giardino":
        area = job.get("area_m2", 0)
        base = max(2.0, round(area / 200, 1))
        return round(base * af + waste_extra, 1), rate

    if s == "raccolta foglie":
        area = job.get("area_m2", 0)
        base = max(1.5, round(area / 250, 1))
        wet_extra = 0.4 if "bagn" in (job.get("notes","").lower()) else 0.0
        return round(base * af + waste_extra + wet_extra, 1), rate

    if s == "smaltimento verde":
        base = 1.5
        return round(base * af, 1), rate

    if s == "trattamenti antiparassitari":
        base = 2.0
        return round(base * af, 1), rate

    return 2.0, rate

def calc_quote(job: dict):
    hours, rate = estimate_hours(job)
    total = round(hours * rate, 2)
    return hours, rate, total

def totals(jobs: list):
    total_h = 0.0
    total_e = 0.0
    breakdown = []
    for j in jobs:
        h, r, e = calc_quote(j)
        total_h += h
        total_e += e
        breakdown.append((j["service"], h, r, e))
    return round(total_h, 1), round(total_e, 2), breakdown

# =========================
# PARSE user answers for pending field (no more losing context)
# =========================
def parse_int(text):
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None

def parse_float(text):
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    return float(m.group(1).replace(",", ".")) if m else None

def yn(text):
    t = text.lower().strip()
    if t in {"si", "sì", "ok", "va bene", "certo"}: return "si"
    if "no" in t: return "no"
    return None

def parse_access(text):
    t = text.lower()
    if "facile" in t: return "facile"
    if "medio" in t: return "medio"
    if "diffic" in t: return "difficile"
    return None

def parse_size(text):
    t = text.lower()
    if "piccol" in t: return "piccolo"
    if "medio" in t: return "medio"
    if "grand" in t: return "grande"
    return None

def apply_pending(job: dict, field: str, user_text: str):
    t = user_text.strip()
    if field in {"length_m", "area_m2", "count"}:
        v = parse_int(t)
        if v: job[field] = v
        return
    if field == "height_m":
        v = parse_float(t)
        if v and v <= 50: job[field] = v
        return
    if field == "access":
        v = parse_access(t)
        if v: job[field] = v
        return
    if field == "waste":
        v = yn(t)
        if v: job[field] = v
        return
    if field in {"obstacles", "parking", "slope", "edges"}:
        v = yn(t)
        if v: job[field] = v
        return
    if field == "size":
        v = parse_size(t)
        if v: job[field] = v
        return
    if field == "volume":
        if len(t) >= 2: job[field] = t
        return
    if field in {"plant", "problem", "notes"}:
        if len(t) >= 2: job[field] = t
        return

# =========================
# AI extraction (solo per capire quali lavori ci sono)
# =========================
EXTRACT_SYSTEM = f"""
Sei un assistente per un giardiniere.
Devi estrarre SOLO: quali lavori/servizi l'utente vuole (anche multipli).
Rispondi SOLO JSON valido. Niente testo extra.

Servizi disponibili:
{list(SERVICES.keys())}

Regole:
- "ulivo" -> "potatura alberi" e specie="ulivo"
- "siepe" -> "potatura siepi"
- "erba/prato" -> "taglio prato"
- "corda" -> "potatura su corda"
"""

def ai_extract_services(message: str):
    schema = {"services": [{"service": "potatura siepi", "species": None}]}
    prompt = f"Messaggio: {message}\nSchema: {json.dumps(schema, ensure_ascii=False)}"
    c = ai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role":"system","content":EXTRACT_SYSTEM},{"role":"user","content":prompt}],
        temperature=0.0,
        max_tokens=200
    )
    txt = c.choices[0].message.content.strip()
    try:
        return json.loads(txt).get("services", [])
    except Exception:
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0)).get("services", [])
            except Exception:
                pass
    return []

def normalize_service(s: str, species: str|None):
    s = (s or "").lower().strip()
    if species and str(species).lower().strip() == "ulivo":
        return "potatura alberi", "ulivo"
    if "ulivo" in s:
        return "potatura alberi", "ulivo"
    if s in SERVICES:
        return s, species
    if "siepe" in s:
        return "potatura siepi", species
    if "prato" in s or "erba" in s:
        return "taglio prato", species
    if "corda" in s:
        return "potatura su corda", species
    if "alber" in s:
        return "potatura alberi", species
    if "foglie" in s:
        return "raccolta foglie", species
    if "pulizia" in s:
        return "pulizia giardino", species
    if "smalt" in s:
        return "smaltimento verde", species
    if "antipar" in s or "tratt" in s:
        return "trattamenti antiparassitari", species
    return None, species

# =========================
# booking preference parsing
# =========================
def detect_booking_pref(text: str):
    low = text.lower()
    day = None
    part = None
    start_time = None

    if "domani" in low:
        day = (dt.datetime.now(TZ) + dt.timedelta(days=1)).date().isoformat()
    if "mattina" in low:
        part = "mattina"
    if "pomeriggio" in low:
        part = "pomeriggio"

    m = re.search(r"(?:alle|dalle)\s*(\d{1,2})(?::(\d{2}))?", low)
    if m:
        h = int(m.group(1))
        mm = int(m.group(2)) if m.group(2) else 0
        start_time = dt.time(hour=h, minute=mm)

    return day, part, start_time

def user_wants_booking(text: str):
    low = text.lower()
    return any(k in low for k in ["quando puoi", "quando sei libero", "puoi venire", "passare", "prenot", "appunt"])

# =========================
# MAIN WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    phone = request.form.get("From")
    user_message = (request.form.get("Body", "") or "").strip()
    if not user_message:
        user_message = "Ciao"

    state = load_session(phone)
    state["short_history"].append({"u": user_message})
    state["short_history"] = state["short_history"][-12:]

    # A) se stiamo aspettando conferma per inserire evento
    if state["phase"] == "BOOK_CONFIRM" and state.get("proposed_slot"):
        if user_message.lower().strip() in CONFIRM_WORDS:
            slot = state["proposed_slot"]
            start_dt = dt.datetime.fromisoformat(slot["start"]).astimezone(TZ)
            end_dt = dt.datetime.fromisoformat(slot["end"]).astimezone(TZ)

            total_h, total_e, breakdown = totals(state["jobs"])

            event_id = None
            if calendar_service:
                desc_lines = [f"Cliente: {phone}", f"Totale ore: {total_h}", f"Totale indicativo: {total_e}€", ""]
                for s, h, r, e in breakdown:
                    desc_lines.append(f"- {s}: {h}h × {r}€/h = {e}€")
                event_id = create_event("Lavori giardino (WhatsApp)", "\n".join(desc_lines), start_dt, end_dt)

            save_history(phone, state["jobs"], total_h, total_e, event_id, start_dt.isoformat(), end_dt.isoformat())

            reply = (
                f"✅ Confermato! Ho inserito l’appuntamento in agenda:\n"
                f"📅 {start_dt.strftime('%d/%m/%Y %H:%M')} → {end_dt.strftime('%H:%M')} (durata {total_h}h)\n"
                f"💶 Totale indicativo: {total_e}€\n\n"
                "Nota: il prezzo finale può variare leggermente dopo sopralluogo (accessi/ostacoli/smaltimento reale)."
            )

            # reset
            state = {"phase":"IDLE","jobs":[],"pending":None,"proposed_slot":None,"short_history":state["short_history"][-6:]}
            save_session(phone, state)

            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

    # B) se abbiamo una domanda pendente -> applica risposta al campo giusto
    if state["phase"] == "COLLECTING" and state.get("pending"):
        job_i = state["pending"]["job_i"]
        field = state["pending"]["field"]
        job = state["jobs"][job_i]
        apply_pending(job, field, user_message)
        state["pending"] = None
        save_session(phone, state)

    # C) Se non abbiamo lavori, estrai servizi dal messaggio
    if not state["jobs"]:
        services = ai_extract_services(user_message)
        for item in services:
            svc, species = normalize_service(item.get("service"), item.get("species"))
            if not svc:
                continue
            state["jobs"].append({
                "service": svc,
                "species": species,
                "length_m": 0,
                "height_m": 0.0,
                "area_m2": 0,
                "count": 0,
                "waste": None,
                "access": None,
                "obstacles": None,
                "parking": None,
                "size": None,
                "slope": None,
                "edges": None,
                "volume": None,
                "plant": None,
                "problem": None,
                "notes": ""
            })

    # Se ancora zero lavori -> chiedi cosa serve
    if not state["jobs"]:
        reply = (
            "Ciao! 😊 Dimmi pure cosa ti serve.\n"
            "Esempi: potatura siepi, potatura ulivi/alberi, taglio prato, pulizia giardino, raccolta foglie, smaltimento verde, trattamenti antiparassitari."
        )
        state["phase"] = "IDLE"
        save_session(phone, state)
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # D) Se manca qualche dato obbligatorio, chiedi (una domanda alla volta)
    for i, job in enumerate(state["jobs"]):
        miss = next_missing(job)
        if miss:
            state["phase"] = "COLLECTING"
            state["pending"] = {"job_i": i, "field": miss}

            lead = ""
            if len(state["jobs"]) > 1:
                lead = f"Perfetto, per un preventivo preciso partiamo da **{job['service']}**.\n"

            reply = lead + question_for(job["service"], miss)
            save_session(phone, state)
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

    # E) Ora abbiamo tutti i dati -> preventivo deterministico
    total_h, total_e, breakdown = totals(state["jobs"])

    lines = []
    for s, h, r, e in breakdown:
        lines.append(f"• {s}: {h}h × {r}€/h = {e}€")
    breakdown_txt = "\n".join(lines)

    quote_msg = (
        "✅ Preventivo indicativo (più accurato possibile con i dati forniti):\n\n"
        f"{breakdown_txt}\n\n"
        f"Totale stimato: **{total_h} ore** — **{total_e}€**\n"
        "Nota: confermo definitivamente dopo sopralluogo se emergono difficoltà non visibili (accessi, ostacoli, smaltimento reale)."
    )

    # F) Se l’utente vuole appuntamento / preferenza giorno
    if user_wants_booking(user_message) or ("domani" in user_message.lower()) or ("pomeriggio" in user_message.lower()) or ("mattina" in user_message.lower()):
        if not calendar_service:
            reply = quote_msg + "\n\n📅 Per fissare l’appuntamento: dimmi 2-3 disponibilità (giorno + fascia) e lo confermo."
            state["phase"] = "QUOTED"
            save_session(phone, state)
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        day_pref, part_pref, start_time = detect_booking_pref(user_message)

        slot = find_slot(total_h, day_pref=day_pref, part_pref=part_pref, start_at=start_time)
        if not slot:
            reply = quote_msg + "\n\n📅 In quella fascia non ho uno slot abbastanza lungo per finire tutto. Vuoi un altro giorno o fascia (mattina/pomeriggio)?"
            state["phase"] = "QUOTED"
            save_session(phone, state)
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        start_dt, end_dt = slot
        state["proposed_slot"] = {"start": start_dt.isoformat(), "end": end_dt.isoformat()}
        state["phase"] = "BOOK_CONFIRM"
        save_session(phone, state)

        reply = (
            quote_msg
            + "\n\n📅 Ho trovato uno slot libero abbastanza lungo per completare tutto:\n"
            f"**{start_dt.strftime('%d/%m/%Y %H:%M')} → {end_dt.strftime('%H:%M')}**\n"
            f"(durata {total_h}h)\n"
            "Vuoi che lo inserisca in agenda? Rispondi **OK**."
        )
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # G) Altrimenti proponi di fissare
    reply = quote_msg + "\n\nVuoi che ti proponga il primo appuntamento libero? Scrivimi: **domani pomeriggio** oppure **quando puoi venire?**"
    state["phase"] = "QUOTED"
    save_session(phone, state)
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

# =========================
# HEALTH CHECK
# =========================
@app.route("/")
def home():
    return "Chatbot giardiniere attivo ✅"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
