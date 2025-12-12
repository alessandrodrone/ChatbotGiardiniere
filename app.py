from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os, re, sqlite3
import datetime as dt
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)
TZ = ZoneInfo("Europe/Rome")

# =========================
# MEMORIA BREVE (SESSIONI)
# =========================
SESSIONS = {}
CONFIRM_WORDS = {"ok", "va bene", "confermo", "confermiamo", "sì", "si", "perfetto", "vai"}

# =========================
# OPENAI (solo per domande botaniche / tono)
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY mancante nelle variabili d'ambiente.")
client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# SERVIZI E PREZZI (€/h)
# =========================
SERVICES = {
    "taglio prato": 25,
    "potatura siepi": 30,
    "potatura alberi": 35,
    "potatura su corda": 60,
    "trattamenti antiparassitari": 40,
    "pulizia giardino": 28,
    "raccolta foglie": 25,
    "smaltimento verde": 30,
}

# =========================
# GOOGLE CALENDAR
# =========================
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

calendar_service = None
try:
    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )
    calendar_service = build("calendar", "v3", credentials=credentials)
except Exception:
    calendar_service = None

# =========================
# DATABASE (memoria lunga)
# =========================
DB_PATH = os.getenv("DB_PATH", "bot.db")

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      phone TEXT NOT NULL,
      created_at TEXT NOT NULL,
      service TEXT,
      length_m INTEGER,
      height_m REAL,
      hours REAL,
      quote_eur REAL,
      start_iso TEXT,
      end_iso TEXT,
      calendar_event_id TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

def save_job(phone, service, length_m, height_m, hours, quote_eur, start_iso=None, end_iso=None, event_id=None):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO jobs (phone, created_at, service, length_m, height_m, hours, quote_eur, start_iso, end_iso, calendar_event_id)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        phone,
        dt.datetime.now(TZ).isoformat(),
        service,
        length_m,
        height_m,
        hours,
        quote_eur,
        start_iso,
        end_iso,
        event_id
    ))
    conn.commit()
    conn.close()

def last_jobs(phone, limit=3):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
      SELECT service, length_m, height_m, quote_eur, start_iso
      FROM jobs
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
        out.append(f"- {r['service']} ({r['length_m']}m, {r['height_m']}m){' • ' + when if when else ''} • {round(r['quote_eur'], 2)}€")
    return "Storico lavori:\n" + "\n".join(out)

# =========================
# ORARI DI LAVORO
# =========================
WORK_DAYS = {0,1,2,3,4}   # Lun-Ven
WORK_START = int(os.getenv("WORK_START_HOUR", "8"))
WORK_END = int(os.getenv("WORK_END_HOUR", "18"))

def part_of_day_window(date_obj, part):
    """
    part: 'mattina' | 'pomeriggio' | None
    """
    if part == "mattina":
        return (dt.datetime(date_obj.year, date_obj.month, date_obj.day, WORK_START, 0, tzinfo=TZ),
                dt.datetime(date_obj.year, date_obj.month, date_obj.day, 13, 0, tzinfo=TZ))
    if part == "pomeriggio":
        return (dt.datetime(date_obj.year, date_obj.month, date_obj.day, 14, 0, tzinfo=TZ),
                dt.datetime(date_obj.year, date_obj.month, date_obj.day, WORK_END, 0, tzinfo=TZ))
    return (dt.datetime(date_obj.year, date_obj.month, date_obj.day, WORK_START, 0, tzinfo=TZ),
            dt.datetime(date_obj.year, date_obj.month, date_obj.day, WORK_END, 0, tzinfo=TZ))

def freebusy(time_min, time_max):
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
        s = dt.datetime.fromisoformat(b["start"].replace("Z","+00:00")).astimezone(TZ)
        e = dt.datetime.fromisoformat(b["end"].replace("Z","+00:00")).astimezone(TZ)
        intervals.append((s,e))
    intervals.sort(key=lambda x: x[0])
    return intervals

def find_slot_in_window(duration_hours, window_start, window_end):
    """
    Trova il primo slot libero tra window_start e window_end che contenga durata.
    """
    duration = dt.timedelta(hours=float(duration_hours))
    busy = freebusy(window_start, window_end)
    cursor = window_start
    for b_start, b_end in busy:
        if b_end <= cursor:
            continue
        if b_start >= window_end:
            break
        free_end = min(b_start, window_end)
        if free_end - cursor >= duration:
            return cursor, cursor + duration
        cursor = max(cursor, b_end)
        if cursor >= window_end:
            break
    if window_end - cursor >= duration:
        return cursor, cursor + duration
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
# PARSING DETTAGLI (deterministico)
# =========================
def parse_length_m(text):
    m = re.search(r"(\d+)\s*metri", text.lower())
    return int(m.group(1)) if m else None

def parse_height_m(text):
    # supporta "2 metri", "2m", "2,5 metri", "2.5m"
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:m|metri)\s*(?:di\s*altezza|alta|alto)?", text.lower())
    if not m:
        # cerca "alta 2"
        m = re.search(r"alta\s*(\d+(?:[.,]\d+)?)", text.lower())
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    # filtro: se qualcuno scrive "300 metri", non vogliamo considerarlo altezza
    if val > 10:
        return None
    return val

def detect_service(text):
    low = text.lower()
    for s in SERVICES:
        if s in low:
            return s
    # alias comuni
    if "siepe" in low:
        return "potatura siepi"
    if "prato" in low or "erba" in low:
        return "taglio prato"
    if "corda" in low:
        return "potatura su corda"
    if "albero" in low or "alberi" in low:
        return "potatura alberi"
    if "foglie" in low:
        return "raccolta foglie"
    if "smalt" in low:
        return "smaltimento verde"
    if "pulizia" in low:
        return "pulizia giardino"
    return None

def detect_time_pref(text):
    low = text.lower()
    day = None
    part = None
    hour = None

    if "domani" in low:
        day = (dt.datetime.now(TZ) + dt.timedelta(days=1)).date()
    elif "oggi" in low:
        day = dt.datetime.now(TZ).date()

    if "mattina" in low:
        part = "mattina"
    if "pomeriggio" in low:
        part = "pomeriggio"

    # "dalle 15", "alle 15", "15:30"
    m = re.search(r"(?:dalle|alle)\s*(\d{1,2})(?::(\d{2}))?", low)
    if m:
        h = int(m.group(1))
        mm = int(m.group(2)) if m.group(2) else 0
        hour = (h, mm)

    m2 = re.search(r"\b(\d{1,2}):(\d{2})\b", low)
    if m2:
        hour = (int(m2.group(1)), int(m2.group(2)))

    return day, part, hour

# =========================
# STIMA ORE (siepi dipende molto dall’altezza)
# =========================
def estimate_hours(service, length_m=None, height_m=None):
    if service == "potatura siepi":
        if not length_m:
            return 3.0
        # velocità base in metri/ora in base all'altezza
        # (valori realistici per NON sottostimare)
        if height_m is None:
            speed = 35  # prudente
        elif height_m <= 1.5:
            speed = 50
        elif height_m <= 2.5:
            speed = 35
        else:
            speed = 25
        return max(2.0, round(length_m / speed, 1))

    if service == "taglio prato":
        return 2.0

    if service == "pulizia giardino":
        return 3.0

    if service == "raccolta foglie":
        return 2.0

    if service == "potatura alberi":
        return 3.0

    if service == "potatura su corda":
        return 4.0

    return 2.0

def quote(service, hours):
    rate = SERVICES.get(service)
    if rate is None:
        return None, None
    total = round(rate * float(hours), 2)
    return rate, total

# =========================
# AI SOLO PER DOMANDE BOTANICHE / SPIEGAZIONI
# =========================
BOTANIC_SYSTEM = f"""
Sei un assistente WhatsApp professionale per un giardiniere.
Parla SOLO di giardinaggio, piante, manutenzione del verde.
Se domanda fuori ambito: rifiuta gentilmente e riporta al giardinaggio.
Rispondi in italiano, pratico, non teorico.
"""

def ai_botanic_answer(user_message, history=""):
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": BOTANIC_SYSTEM},
            {"role": "user", "content": f"Contesto:\n{history}\n\nMessaggio:\n{user_message}"},
        ],
        temperature=0.3,
        max_tokens=450,
    )
    return completion.choices[0].message.content.strip()

# =========================
# WHATSAPP WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    phone = request.form.get("From")
    user_message = (request.form.get("Body", "") or "").strip()
    if not user_message:
        user_message = "Ciao"

    session = SESSIONS.get(phone, {
        "state": "idle",
        "service": None,
        "length_m": None,
        "height_m": None,
        "hours": None,
        "quote_total": None,
        "pref_day": None,
        "pref_part": None,
        "pref_hour": None,
        "slot_start": None,
        "slot_end": None,
    })

    history_long = last_jobs(phone)

    # -------------------------
    # 1) Se stiamo aspettando un orario ("dalle 15") o conferma
    # -------------------------
    if session["state"] == "awaiting_time_choice":
        day, part, hour = detect_time_pref(user_message)
        # l'utente spesso risponde solo "dalle 15": usa day/part già scelti prima
        if hour:
            session["pref_hour"] = hour
        if part:
            session["pref_part"] = part
        if day:
            session["pref_day"] = day

        # serve almeno il giorno + una fascia (pomeriggio/mattina) o un orario
        if not session["pref_day"]:
            reply = "Perfetto 😊 Mi dici che giorno preferisci? (es. domani, lunedì, martedì)"
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        if not session["pref_part"] and not session["pref_hour"]:
            reply = "Ottimo. Preferisci mattina o pomeriggio?"
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        # calcolo finestra
        day_obj = session["pref_day"]
        if isinstance(day_obj, str):
            day_obj = dt.date.fromisoformat(day_obj)
        w_start, w_end = part_of_day_window(day_obj, session["pref_part"])

        # se ha scelto un orario specifico, restringi finestra da quell'ora
        if session["pref_hour"]:
            h, m = session["pref_hour"]
            chosen = dt.datetime(day_obj.year, day_obj.month, day_obj.day, h, m, tzinfo=TZ)
            if chosen < w_start:
                chosen = w_start
            w_start = chosen

        if not calendar_service:
            reply = "Per ora non riesco a leggere la tua agenda automaticamente. Dimmi tu un paio di orari e confermo manualmente."
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        slot = find_slot_in_window(session["hours"], w_start, w_end)
        if not slot:
            reply = "In quella fascia non ho uno spazio abbastanza lungo per finire il lavoro. Vuoi provare un altro giorno o un’altra fascia?"
            session["state"] = "awaiting_day_part"
            SESSIONS[phone] = session
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        start_dt, end_dt = slot
        session["slot_start"] = start_dt.isoformat()
        session["slot_end"] = end_dt.isoformat()
        session["state"] = "awaiting_confirmation"
        SESSIONS[phone] = session

        when_txt = start_dt.strftime("%d/%m/%Y %H:%M")
        reply = (
            f"Perfetto ✅ Ho trovato uno slot libero abbastanza lungo:\n"
            f"📅 **{when_txt}** (durata {session['hours']}h)\n"
            f"Vuoi che lo confermi e lo inserisca in agenda? Rispondi **OK**."
        )
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    if session["state"] == "awaiting_confirmation":
        if user_message.lower() in CONFIRM_WORDS:
            if not calendar_service:
                reply = "Non riesco a inserire in agenda in automatico al momento. Dimmi tu un orario e lo segno manualmente."
                resp = MessagingResponse()
                resp.message(reply)
                return str(resp)

            start_dt = dt.datetime.fromisoformat(session["slot_start"]).astimezone(TZ)
            end_dt = dt.datetime.fromisoformat(session["slot_end"]).astimezone(TZ)

            summary = f"{session['service']} - Cliente WhatsApp"
            desc = (
                f"Cliente: {phone}\n"
                f"Siepe: {session.get('length_m')}m, altezza: {session.get('height_m')}m\n"
                f"Ore stimate: {session['hours']}\n"
                f"Preventivo indicativo: {session['quote_total']}€\n"
            )
            event_id = None
            try:
                event_id = create_event(summary, desc, start_dt, end_dt)
            except Exception:
                event_id = None

            save_job(
                phone=phone,
                service=session["service"],
                length_m=session.get("length_m"),
                height_m=session.get("height_m"),
                hours=session["hours"],
                quote_eur=session["quote_total"],
                start_iso=start_dt.isoformat(),
                end_iso=end_dt.isoformat(),
                event_id=event_id
            )

            reply = (
                f"✅ Appuntamento confermato!\n"
                f"📅 {start_dt.strftime('%d/%m/%Y %H:%M')} (durata {session['hours']}h)\n"
                f"💶 Preventivo indicativo: {session['quote_total']}€\n\n"
                "Se vuoi, mandami indirizzo e qualche foto della siepe così arrivo già preparato."
            )

            # reset session breve
            SESSIONS[phone] = {
                "state": "idle",
                "service": None,
                "length_m": None,
                "height_m": None,
                "hours": None,
                "quote_total": None,
                "pref_day": None,
                "pref_part": None,
                "pref_hour": None,
                "slot_start": None,
                "slot_end": None,
            }

            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)
        # se non è conferma, continua normalmente senza perdere lo stato (es: chiede info)
        # prosegue...

    # -------------------------
    # 2) Riconosci intent “appuntamento” / “preventivo” / info
    # -------------------------
    low = user_message.lower()

    wants_booking = any(k in low for k in ["quando puoi", "quando sei libero", "passare", "venire", "appunt", "prenot"])
    wants_quote = any(k in low for k in ["preventivo", "quanto costa", "prezzo", "costo", "mi fai un preventivo"])

    service = detect_service(user_message)
    length_m = parse_length_m(user_message)
    height_m = parse_height_m(user_message)

    # aggiorna sessione con quello che trova
    if service:
        session["service"] = service
    if length_m:
        session["length_m"] = length_m
    if height_m:
        session["height_m"] = height_m

    # -------------------------
    # 3) Se potatura siepi e manca altezza -> chiedi altezza (il tuo punto!)
    # -------------------------
    if session["service"] == "potatura siepi" and session["length_m"] and not session["height_m"]:
        session["state"] = "awaiting_height"
        SESSIONS[phone] = session
        reply = (
            f"Perfetto 😊 Ho capito: **potatura siepi** di circa **{session['length_m']} metri**.\n"
            "Per stimare bene ore e prezzo mi dici l’altezza media?\n"
            "Esempi: **1 m**, **2 m**, **3 m** (anche approssimativo va benissimo)."
        )
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    if session["state"] == "awaiting_height":
        h = parse_height_m(user_message)
        if not h:
            reply = "Ok! Mi dai solo l’altezza media in metri? (es. 2 m)"
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)
        session["height_m"] = h
        session["state"] = "idle"
        SESSIONS[phone] = session
        # ora può fare preventivo accurato

    # -------------------------
    # 4) Se è una domanda botanica (e non preventivo/appuntamento) -> rispondi via AI
    # -------------------------
    is_probably_info = (not wants_quote and not wants_booking and not session["service"])
    if is_probably_info:
        reply = ai_botanic_answer(user_message, history_long)
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # -------------------------
    # 5) Preventivo deterministico (ore coerenti, NON inventate)
    # -------------------------
    if session["service"]:
        # ore stimate
        hours = estimate_hours(session["service"], session.get("length_m"), session.get("height_m"))
        rate, total = quote(session["service"], hours)
        session["hours"] = hours
        session["quote_total"] = total

        SESSIONS[phone] = session

        # risposta preventivo
        details_txt = []
        if session.get("length_m"):
            details_txt.append(f"{session['length_m']} m")
        if session.get("height_m"):
            details_txt.append(f"altezza {session['height_m']} m")
        details_str = ", ".join(details_txt) if details_txt else "dettagli da definire"

        preventivo = (
            f"Perfetto! Per **{session['service']}** ({details_str}) stimerei circa **{hours} ore**.\n"
            f"💶 {hours}h × {rate}€/h = **{total}€** (preventivo indicativo).\n"
        )

        # -------------------------
        # 6) Appuntamento: NON inventiamo slot, usiamo Calendar e durata reale
        # -------------------------
        if wants_booking:
            if not calendar_service:
                reply = preventivo + "\n📅 Al momento non riesco a leggere l’agenda automaticamente. Dimmi 2-3 disponibilità e confermo."
                resp = MessagingResponse()
                resp.message(reply)
                return str(resp)

            # se l'utente ha indicato preferenze (domani pomeriggio / dalle 15)
            pref_day, pref_part, pref_hour = detect_time_pref(user_message)
            if pref_day:
                session["pref_day"] = pref_day.isoformat()
            if pref_part:
                session["pref_part"] = pref_part
            if pref_hour:
                session["pref_hour"] = pref_hour

            # se mancano preferenze, chiedile
            if not session.get("pref_day"):
                session["state"] = "awaiting_day_part"
                SESSIONS[phone] = session
                reply = preventivo + "\n📅 Per fissare l’appuntamento: preferisci **domani** o **settimana prossima**?"
                resp = MessagingResponse()
                resp.message(reply)
                return str(resp)

            if not session.get("pref_part") and not session.get("pref_hour"):
                session["state"] = "awaiting_time_choice"
                SESSIONS[phone] = session
                reply = preventivo + "\nPerfetto. Preferisci **mattina** o **pomeriggio**?"
                resp = MessagingResponse()
                resp.message(reply)
                return str(resp)

            # abbiamo abbastanza per cercare slot: passiamo a awaiting_time_choice e facciamo lo slot sul prossimo messaggio
            session["state"] = "awaiting_time_choice"
            SESSIONS[phone] = session
            reply = preventivo + "\nOk. Dimmi un orario indicativo (es. **dalle 15**) e ti propongo lo slot libero giusto (con durata completa)."
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        # se non chiede appuntamento, proponilo senza inventare
        reply = preventivo + "\nVuoi anche fissare un appuntamento? Scrivimi: **quando puoi passare**."
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # -------------------------
    # fallback
    # -------------------------
    reply = ai_botanic_answer(user_message, history_long)
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
