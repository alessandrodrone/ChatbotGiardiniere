from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os, re, json, sqlite3
import datetime as dt
from zoneinfo import ZoneInfo

# Google Calendar
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
client = OpenAI(api_key=OPENAI_API_KEY)

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
    "potatura alberi": 35,          # include ulivo come "alberi"
    "potatura su corda": 60,
    "trattamenti antiparassitari": 40,
    "pulizia giardino": 28,
    "raccolta foglie": 25,
    "smaltimento verde": 30,
}

# =========================
# DB
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
        # default state
        return {
            "phase": "idle",              # idle | collecting | quoting | proposing_slot | awaiting_confirm
            "jobs": [],                   # list of jobs dict
            "current_job": 0,             # index
            "pending_question": None,     # last question asked
            "proposed_slot": None,        # {"start": iso, "end": iso}
            "history": []                 # short memory: last N turns
        }
    try:
        return json.loads(row["state_json"])
    except Exception:
        return {
            "phase": "idle",
            "jobs": [],
            "current_job": 0,
            "pending_question": None,
            "proposed_slot": None,
            "history": []
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

def save_history_record(phone: str, jobs: list, total_hours: float, total_quote: float,
                        event_id: str | None, start_iso: str | None, end_iso: str | None):
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
        out.append(f"- Lavori: {r['jobs_json']} • Totale: {round(r['total_quote_eur'],2)}€{' • ' + when if when else ''}")
    return "Storico recente:\n" + "\n".join(out)

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

def next_work_windows(days_ahead=10):
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

def find_first_slot(duration_hours: float):
    duration = dt.timedelta(hours=float(duration_hours))
    windows = next_work_windows(10)
    if not windows:
        return None
    overall_min = windows[0][0]
    overall_max = windows[-1][1]
    busy = freebusy(overall_min, overall_max)

    for w_start, w_end in windows:
        cursor = w_start
        for b_start, b_end in busy:
            if b_end <= cursor:
                continue
            if b_start >= w_end:
                break
            free_end = min(b_start, w_end)
            if free_end - cursor >= duration:
                return (cursor, cursor + duration)
            cursor = max(cursor, b_end)
            if cursor >= w_end:
                break
        if w_end - cursor >= duration:
            return (cursor, cursor + duration)
    return None

def create_calendar_event(summary, description, start_dt, end_dt):
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
# AI: extraction (JSON)
# =========================
EXTRACT_SYSTEM = f"""
Sei un assistente per un giardiniere. Devi estrarre informazioni strutturate dal messaggio.
Rispondi SOLO con JSON valido, senza testo extra.

Servizi possibili:
{list(SERVICES.keys())}

Se trovi "ulivo", consideralo come "potatura alberi" e metti specie="ulivo".
Se trovi più lavori, elencali tutti.
"""

def ai_extract(message: str, short_history: list, long_history: str):
    schema = {
        "intent": "info|quote|book|mixed",
        "jobs": [
            {
                "service": "potatura siepi",
                "species": "ulivo|...|null",
                "length_m": 0,
                "height_m": 0.0,
                "area_m2": 0,
                "count": 0,
                "waste": "si|no|non_so",
                "access": "facile|medio|difficile|non_so",
                "notes": "string"
            }
        ],
        "booking": {
            "wants_booking": True,
            "time_pref": "domani|settimana prossima|non so|null"
        },
        "question": "se è domanda botanica, mettila qui, altrimenti null"
    }

    prompt = f"""
Restituisci SOLO JSON.
Schema:
{json.dumps(schema, ensure_ascii=False)}

Memoria breve (ultimi messaggi):
{json.dumps(short_history[-6:], ensure_ascii=False)}

Memoria lunga (storico):
{long_history or ""}

Messaggio utente:
{message}
"""

    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=380,
    )
    txt = completion.choices[0].message.content.strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"intent": "mixed", "jobs": [], "booking": {"wants_booking": False, "time_pref": None}, "question": None}

# =========================
# BOTANIC AI (freeform)
# =========================
BOTANIC_SYSTEM = """
Sei un assistente WhatsApp professionale per un giardiniere.
Parla SOLO di giardinaggio e piante. Risposte pratiche, concise e affidabili.
Se serve sopralluogo, dillo.
"""

def ai_botanic_answer(message: str, short_history: list, long_history: str):
    prompt = f"""
Memoria breve:
{json.dumps(short_history[-6:], ensure_ascii=False)}

Storico:
{long_history or ""}

Cliente:
{message}

Rispondi solo in ambito giardinaggio.
"""
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": BOTANIC_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.35,
        max_tokens=450,
    )
    return completion.choices[0].message.content.strip()

# =========================
# CHECKLIST (dati obbligatori per preventivi seri)
# =========================
def required_fields_for(job: dict):
    s = job.get("service")
    req = []
    if s == "potatura siepi":
        req = ["length_m", "height_m", "access", "waste"]
    elif s == "potatura alberi":
        # ulivo incluso
        req = ["count", "access", "waste"]
    elif s == "taglio prato":
        req = ["area_m2", "access"]
    elif s == "pulizia giardino":
        req = ["area_m2", "waste", "access"]
    elif s == "raccolta foglie":
        req = ["area_m2", "waste"]
    elif s == "smaltimento verde":
        req = ["notes"]  # volume/numero sacchi in notes
    elif s == "trattamenti antiparassitari":
        req = ["notes"]  # tipo pianta + problema in notes
    else:
        req = ["notes"]
    return req

def is_missing(job: dict, field: str):
    v = job.get(field)
    if field in {"length_m", "area_m2", "count"}:
        return not isinstance(v, int) or v <= 0
    if field == "height_m":
        return not isinstance(v, (int, float)) or v <= 0
    if field in {"access", "waste"}:
        return v not in {"facile", "medio", "difficile", "si", "no"} and v not in {"facile","medio","difficile"} and v not in {"si","no","non_so"}
    if field == "notes":
        return not v or len(str(v).strip()) < 3
    return v is None

def next_missing_question(job: dict):
    s = job.get("service")
    for f in required_fields_for(job):
        if is_missing(job, f):
            if s == "potatura siepi":
                if f == "length_m":
                    return "Quanti metri è lunga la siepe circa?"
                if f == "height_m":
                    return "Altezza media della siepe? (es. 1,5m / 2m / 3m)"
                if f == "access":
                    return "Accesso com’è? (facile = spazio e accesso comodi / medio / difficile)"
                if f == "waste":
                    return "Vuoi anche smaltimento del verde? (sì/no)"
            if s == "potatura alberi":
                if f == "count":
                    return "Quanti alberi/ulivi sono da potare?"
                if f == "access":
                    return "Accesso com’è? (facile / medio / difficile)"
                if f == "waste":
                    return "Vuoi anche smaltimento del verde? (sì/no)"
            if s == "taglio prato":
                if f == "area_m2":
                    return "Quanti mq circa di prato? (es. 200 mq)"
                if f == "access":
                    return "Accesso com’è? (facile / medio / difficile)"
            if s == "pulizia giardino":
                if f == "area_m2":
                    return "Quanto è grande l’area da pulire? (mq circa)"
                if f == "waste":
                    return "Vuoi anche smaltimento del verde? (sì/no)"
                if f == "access":
                    return "Accesso com’è? (facile / medio / difficile)"
            if s == "raccolta foglie":
                if f == "area_m2":
                    return "Su che area circa? (mq)"
                if f == "waste":
                    return "Vuoi anche smaltimento del verde? (sì/no)"
            if s == "trattamenti antiparassitari":
                return "Che pianta è e che problema vedi? (es. afidi, cocciniglia, foglie macchiate)"
            if s == "smaltimento verde":
                return "Indicami circa quanto verde da smaltire (es. 10 sacchi / 1 m³)."
            return "Mi dai un dettaglio in più sul lavoro (misure, accesso, smaltimento)?"
    return None

# =========================
# STIMA ORE (deterministica e prudente)
# =========================
def estimate_hours(job: dict):
    s = job.get("service")
    access = job.get("access")
    waste = job.get("waste")

    access_factor = 1.0
    if access == "medio":
        access_factor = 1.15
    elif access == "difficile":
        access_factor = 1.35

    waste_extra = 0.0
    if waste == "si":
        waste_extra = 0.6  # mezz’ora/1h extra medio (poi si affina)

    if s == "potatura siepi":
        length_m = int(job.get("length_m") or 0)
        height_m = float(job.get("height_m") or 0)

        # velocità prudente in m/h (dipende dall’altezza)
        if height_m <= 1.5:
            speed = 55
        elif height_m <= 2.5:
            speed = 38
        else:
            speed = 28

        base = max(2.0, round(length_m / speed, 1))
        return round(base * access_factor + waste_extra, 1)

    if s == "potatura alberi":
        count = int(job.get("count") or 0)
        # potatura ulivo tipica: 1.5h–3h cad, prudente 2.2h
        per_tree = 2.2
        base = max(1.5, round(count * per_tree, 1))
        return round(base * access_factor + waste_extra, 1)

    if s == "taglio prato":
        area = int(job.get("area_m2") or 0)
        base = max(1.0, round(area / 350, 1))  # 350 mq/h prudente
        return round(base * access_factor, 1)

    if s == "pulizia giardino":
        area = int(job.get("area_m2") or 0)
        base = max(2.0, round(area / 200, 1))
        return round(base * access_factor + waste_extra, 1)

    if s == "raccolta foglie":
        area = int(job.get("area_m2") or 0)
        base = max(1.5, round(area / 250, 1))
        return round(base * access_factor + waste_extra, 1)

    if s == "trattamenti antiparassitari":
        # senza dati, stima prudente
        return round(2.0 * access_factor, 1)

    if s == "smaltimento verde":
        return 1.5

    return 2.0

def calc_quote(job: dict):
    s = job.get("service")
    rate = SERVICES.get(s, 30)
    hours = estimate_hours(job)
    total = round(rate * hours, 2)
    return rate, hours, total

def jobs_ready(jobs: list):
    # tutti i jobs hanno tutti i required compilati
    for j in jobs:
        if not j.get("service"):
            return False
        q = next_missing_question(j)
        if q:
            return False
    return True

def total_estimate(jobs: list):
    total_h = 0.0
    total_e = 0.0
    breakdown = []
    for j in jobs:
        rate, hours, eur = calc_quote(j)
        total_h += hours
        total_e += eur
        breakdown.append((j["service"], hours, rate, eur))
    return round(total_h, 1), round(total_e, 2), breakdown

# =========================
# UTIL: normalize updates from user
# =========================
def parse_int_maybe(text):
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None

def parse_float_maybe(text):
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    return float(m.group(1).replace(",", ".")) if m else None

def apply_user_answer_to_job(job: dict, question: str, user_text: str):
    low = user_text.lower().strip()

    # euristiche: se chiediamo "metri" -> length_m
    if "metri" in question.lower():
        v = parse_int_maybe(low)
        if v:
            job["length_m"] = v
            return

    if "altezza" in question.lower():
        v = parse_float_maybe(low)
        if v and v <= 10:
            job["height_m"] = v
            return

    if "quanti" in question.lower() and ("alberi" in question.lower() or "ulivi" in question.lower()):
        v = parse_int_maybe(low)
        if v:
            job["count"] = v
            return

    if "mq" in question.lower() or "metri quad" in question.lower():
        v = parse_int_maybe(low)
        if v:
            job["area_m2"] = v
            return

    if "accesso" in question.lower():
        if "facile" in low:
            job["access"] = "facile"
            return
        if "medio" in low:
            job["access"] = "medio"
            return
        if "diffic" in low:
            job["access"] = "difficile"
            return

    if "smalt" in question.lower():
        if low in {"si", "sì", "ok", "va bene", "certo"}:
            job["waste"] = "si"
            return
        if "no" in low:
            job["waste"] = "no"
            return

    # fallback notes
    job["notes"] = user_text.strip()

# =========================
# MAIN WHATSAPP WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    phone = request.form.get("From")  # es: whatsapp:+39...
    user_message = (request.form.get("Body", "") or "").strip()
    if not user_message:
        user_message = "Ciao"

    state = load_session(phone)
    long_hist = last_history_summary(phone)
    state["history"].append({"role": "user", "content": user_message})
    state["history"] = state["history"][-12:]  # limita

    # 1) Se stiamo aspettando conferma appuntamento -> OK crea evento
    if state.get("phase") == "awaiting_confirm" and state.get("proposed_slot"):
        if user_message.lower() in CONFIRM_WORDS:
            slot = state["proposed_slot"]
            start_dt = dt.datetime.fromisoformat(slot["start"]).astimezone(TZ)
            end_dt = dt.datetime.fromisoformat(slot["end"]).astimezone(TZ)

            total_h, total_e, breakdown = total_estimate(state["jobs"])

            event_id = None
            if calendar_service:
                summary = "Lavori giardino - Cliente WhatsApp"
                desc_lines = [f"Cliente: {phone}", f"Totale ore stimate: {total_h}", f"Totale indicativo: {total_e}€", ""]
                for s, h, r, e in breakdown:
                    desc_lines.append(f"- {s}: {h}h × {r}€/h = {e}€")
                desc = "\n".join(desc_lines)
                try:
                    event_id = create_calendar_event(summary, desc, start_dt, end_dt)
                except Exception:
                    event_id = None

            save_history_record(
                phone=phone,
                jobs=state["jobs"],
                total_hours=total_h,
                total_quote=total_e,
                event_id=event_id,
                start_iso=start_dt.isoformat(),
                end_iso=end_dt.isoformat(),
            )

            reply = (
                f"✅ Appuntamento confermato!\n"
                f"📅 {start_dt.strftime('%d/%m/%Y %H:%M')} (durata {total_h}h)\n"
                f"💶 Totale indicativo: {total_e}€\n\n"
                "Se vuoi, mandami indirizzo e qualche foto (siepe/ulivo) così arrivo già preparato.\n"
                "Nota: il prezzo finale può variare leggermente dopo sopralluogo (accessi, spazi, smaltimento reale)."
            )

            # reset session (ma lasciamo history breve pulita)
            state = {
                "phase": "idle",
                "jobs": [],
                "current_job": 0,
                "pending_question": None,
                "proposed_slot": None,
                "history": state["history"][-6:]
            }
            save_session(phone, state)

            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

    # 2) Se stavamo raccogliendo dati e avevamo una domanda pendente -> applica risposta
    if state.get("phase") == "collecting" and state.get("pending_question") and state.get("jobs"):
        j = state["jobs"][state.get("current_job", 0)]
        apply_user_answer_to_job(j, state["pending_question"], user_message)
        state["pending_question"] = None

    # 3) Estrazione: capire intent + eventuali nuovi lavori (anche multipli)
    extracted = ai_extract(user_message, state["history"], long_hist)

    intent = extracted.get("intent") or "mixed"
    jobs_from_ai = extracted.get("jobs") or []
    wants_booking = bool((extracted.get("booking") or {}).get("wants_booking"))

    # 4) Se è domanda botanica pura e non stiamo facendo preventivi -> rispondi bene
    if intent == "info" and not jobs_from_ai and not state.get("jobs"):
        reply = ai_botanic_answer(user_message, state["history"], long_hist)
        state["history"].append({"role": "assistant", "content": reply})
        save_session(phone, state)
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # 5) Merge lavori: se AI trova lavori, aggiungili o aggiorna quelli esistenti
    # Normalizza job base
    def normalize_job(j):
        service = (j.get("service") or "").lower().strip()
        # alias: ulivo -> potatura alberi
        if j.get("species") and str(j["species"]).lower().strip() == "ulivo":
            service = "potatura alberi"
        if "ulivo" in (j.get("notes") or "").lower():
            service = "potatura alberi"
            j["species"] = "ulivo"
        if service not in SERVICES:
            # fallback: prova a mappare
            if "siepe" in service:
                service = "potatura siepi"
            elif "prato" in service:
                service = "taglio prato"
            elif "corda" in service:
                service = "potatura su corda"
            elif "alber" in service or "ulivo" in service:
                service = "potatura alberi"
            else:
                service = None
        return {
            "service": service,
            "species": j.get("species"),
            "length_m": int(j.get("length_m") or 0),
            "height_m": float(j.get("height_m") or 0.0),
            "area_m2": int(j.get("area_m2") or 0),
            "count": int(j.get("count") or 0),
            "waste": (j.get("waste") or "non_so"),
            "access": (j.get("access") or "non_so"),
            "notes": (j.get("notes") or "").strip()
        }

    if jobs_from_ai:
        new_jobs = [normalize_job(j) for j in jobs_from_ai]
        # se non avevamo job in corso, impostali
        if not state.get("jobs"):
            # filtra quelli senza service
            state["jobs"] = [j for j in new_jobs if j.get("service")]
            state["current_job"] = 0
        else:
            # aggiorna: per semplicità append se service diverso
            existing_services = {j.get("service") for j in state["jobs"]}
            for j in new_jobs:
                if j.get("service") and j["service"] not in existing_services:
                    state["jobs"].append(j)

    # Se ancora nessun lavoro riconosciuto -> chiedi
    if not state.get("jobs"):
        reply = (
            "Certo 😊 Mi dici che lavoro ti serve?\n"
            "Esempi: potatura siepi, potatura alberi/ulivo, taglio prato, pulizia giardino, raccolta foglie, smaltimento verde."
        )
        state["phase"] = "idle"
        state["history"].append({"role": "assistant", "content": reply})
        save_session(phone, state)
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # 6) Raccolta dati obbligatori: una domanda alla volta (non va in tilt)
    # trova il primo job con campi mancanti
    missing_job_idx = None
    missing_q = None
    for idx, job in enumerate(state["jobs"]):
        q = next_missing_question(job)
        if q:
            missing_job_idx = idx
            missing_q = q
            break

    if missing_q:
        state["phase"] = "collecting"
        state["current_job"] = missing_job_idx
        state["pending_question"] = missing_q

        # se ci sono più lavori, guidiamo: "Ora siepe, poi ulivo"
        lead = ""
        if len(state["jobs"]) > 1:
            lead = f"Perfetto, così ti faccio un preventivo più preciso. Partiamo da **{state['jobs'][missing_job_idx]['service']}**.\n"

        reply = lead + missing_q
        state["history"].append({"role": "assistant", "content": reply})
        save_session(phone, state)
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # 7) A questo punto abbiamo dati sufficienti -> preventivo completo (multi-lavoro)
    total_h, total_e, breakdown = total_estimate(state["jobs"])

    lines = []
    for s, h, r, e in breakdown:
        lines.append(f"• {s}: {h}h × {r}€/h = {e}€")
    breakdown_txt = "\n".join(lines)

    quote_msg = (
        "Perfetto ✅ Ecco una stima indicativa (da confermare al sopralluogo se emergono difficoltà/accessi/smaltimento maggiore):\n\n"
        f"{breakdown_txt}\n\n"
        f"Totale stimato: **{total_h} ore** — **{total_e}€**\n"
    )

    # 8) Prenotazione: se l’utente la chiede ORA, oppure se scrive “quando puoi venire”
    low = user_message.lower()
    implied_booking = any(k in low for k in ["quando puoi", "quando sei libero", "puoi venire", "passare", "appunt", "prenot"])

    if wants_booking or implied_booking:
        if not calendar_service:
            reply = quote_msg + "\n📅 Per fissare l’appuntamento: dimmi 2-3 disponibilità (giorno + fascia) e lo confermo."
            state["phase"] = "quoting"
            state["history"].append({"role": "assistant", "content": reply})
            save_session(phone, state)
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        slot = find_first_slot(total_h)
        if not slot:
            reply = quote_msg + "\n📅 In questi giorni ho l’agenda piena. Dimmi che giorno preferisci (mattina/pomeriggio) e provo a incastrarlo."
            state["phase"] = "quoting"
            state["history"].append({"role": "assistant", "content": reply})
            save_session(phone, state)
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        start_dt, end_dt = slot
        state["proposed_slot"] = {"start": start_dt.isoformat(), "end": end_dt.isoformat()}
        state["phase"] = "awaiting_confirm"
        save_session(phone, state)

        reply = (
            quote_msg
            + "\n📅 Ho trovato il primo slot libero abbastanza lungo per completare tutto:\n"
            f"**{start_dt.strftime('%d/%m/%Y %H:%M')}** (durata {total_h}h)\n"
            "Vuoi che lo inserisca in agenda? Rispondi **OK**."
        )
        state["history"].append({"role": "assistant", "content": reply})
        save_session(phone, state)
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # 9) Se non chiede appuntamento: proponi passo successivo
    reply = quote_msg + "\nVuoi che ti proponga il primo appuntamento libero? Scrivimi: **quando puoi venire?**"
    state["phase"] = "quoting"
    state["history"].append({"role": "assistant", "content": reply})
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
