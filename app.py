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
# MEMORIA BREVE (in RAM)
# =========================
SESSIONS = {}
CONFIRM_WORDS = {"ok", "va bene", "confermiamo", "sì", "si", "perfetto", "conferma", "va bene così", "andiamo"}

# =========================
# OPENAI
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
# GOOGLE CALENDAR (Service Account)
# =========================
# Usa credentials.json presente nel progetto (come stai già facendo).
# IMPORTANTE: il calendario deve essere condiviso con l’email del service account (client_email),
# con permesso "Modifica eventi".
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
    # Se Google non è configurato bene, il bot continuerà a rispondere ma non potrà fissare eventi.
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
        details TEXT,
        hours REAL,
        quote_eur REAL,
        calendar_event_id TEXT,
        start_iso TEXT,
        end_iso TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

def save_job(phone, service, details, hours, quote_eur, event_id=None, start_iso=None, end_iso=None):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO jobs (phone, created_at, service, details, hours, quote_eur, calendar_event_id, start_iso, end_iso)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        phone,
        dt.datetime.now(TZ).isoformat(),
        service,
        details,
        hours,
        quote_eur,
        event_id,
        start_iso,
        end_iso
    ))
    conn.commit()
    conn.close()

def last_jobs_summary(phone, limit=3):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT service, details, quote_eur, start_iso
        FROM jobs
        WHERE phone = ?
        ORDER BY id DESC
        LIMIT ?
    """, (phone, limit))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return "Nessun lavoro precedente registrato per questo numero."
    parts = []
    for r in rows:
        when = r["start_iso"]
        when_txt = ""
        if when:
            try:
                d = dt.datetime.fromisoformat(when).astimezone(TZ)
                when_txt = d.strftime("%d/%m/%Y %H:%M")
            except Exception:
                when_txt = when
        parts.append(f"- {r['service'] or 'lavoro'} ({r['details'] or 'dettagli non salvati'})"
                     f"{' • ' + when_txt if when_txt else ''}"
                     f"{' • ' + str(round(r['quote_eur'], 2)) + '€' if r['quote_eur'] is not None else ''}")
    return "Ultimi lavori:\n" + "\n".join(parts)

# =========================
# ORARI DI LAVORO (semplici)
# =========================
WORK_DAYS = {0, 1, 2, 3, 4}  # Lun-Ven
WORK_START_HOUR = int(os.getenv("WORK_START_HOUR", "8"))
WORK_END_HOUR = int(os.getenv("WORK_END_HOUR", "18"))
SLOT_MINUTES = 30

def next_working_windows(days_ahead=10):
    """Ritorna finestre lavorative nei prossimi N giorni."""
    now = dt.datetime.now(TZ)
    windows = []
    for i in range(days_ahead):
        day = (now + dt.timedelta(days=i)).date()
        if day.weekday() not in WORK_DAYS:
            continue
        start = dt.datetime(day.year, day.month, day.day, WORK_START_HOUR, 0, tzinfo=TZ)
        end = dt.datetime(day.year, day.month, day.day, WORK_END_HOUR, 0, tzinfo=TZ)
        # se oggi e siamo già oltre lo start, parte da adesso arrotondato
        if i == 0 and now > start:
            rounded = now.replace(second=0, microsecond=0)
            # arrotonda a SLOT_MINUTES
            minute = (rounded.minute // SLOT_MINUTES + (1 if rounded.minute % SLOT_MINUTES else 0)) * SLOT_MINUTES
            if minute == 60:
                rounded = rounded.replace(minute=0) + dt.timedelta(hours=1)
            else:
                rounded = rounded.replace(minute=minute)
            start = max(start, rounded)
        if start < end:
            windows.append((start, end))
    return windows

def freebusy_between(time_min, time_max):
    """Ritorna lista intervalli occupati dal calendario (solo se calendar_service disponibile)."""
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
        try:
            s = dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(TZ)
            e = dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(TZ)
            intervals.append((s, e))
        except Exception:
            pass
    intervals.sort(key=lambda x: x[0])
    return intervals

def find_first_slot(duration_hours):
    """Trova primo slot libero in calendario che contenga la durata."""
    duration = dt.timedelta(hours=float(duration_hours))
    windows = next_working_windows(days_ahead=10)
    if not windows:
        return None

    # Intervallo complessivo per freebusy (min->max)
    overall_min = windows[0][0]
    overall_max = windows[-1][1]
    busy = freebusy_between(overall_min, overall_max)

    # Helper: sottrai busy da finestra lavorativa
    for w_start, w_end in windows:
        cursor = w_start
        for b_start, b_end in busy:
            if b_end <= cursor:
                continue
            if b_start >= w_end:
                break
            # spazio libero prima dell'occupato
            free_end = min(b_start, w_end)
            if free_end - cursor >= duration:
                return (cursor, cursor + duration)
            # sposta cursor alla fine del busy
            cursor = max(cursor, b_end)
            if cursor >= w_end:
                break
        # spazio libero dopo ultimo busy
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
# AI: estrazione intenzione + dati (robusta)
# =========================
SYSTEM_PROMPT = f"""
Sei un assistente WhatsApp professionale dedicato ESCLUSIVAMENTE a un giardiniere.
Parla solo di giardinaggio, preventivi, appuntamenti, servizi e piante.

REGOLE:
- Se domanda fuori ambito: rifiuta gentilmente e riporta al giardinaggio.
- Se l’utente chiede preventivo: identifica servizio/i tra quelli disponibili, stima ore se possibile o chiedi 1 dettaglio essenziale.
- Se l’utente vuole appuntamento: proponi o conferma, usando la disponibilità che ti viene passata dall'app (non inventare date a caso).

Servizi disponibili e prezzi €/h:
{SERVICES}
"""

def ai_extract(user_message, history_summary=None, last_jobs=None):
    """
    Estrae in JSON: intent, services, details, hours_hint, wants_booking, wants_quote, question.
    """
    instruction = {
        "intent": "info|quote|book|mixed",
        "services": ["..."],
        "details": "string breve (es. 'siepe 300 metri, altezza 2m')",
        "hours_hint": "numero o null",
        "wants_quote": True,
        "wants_booking": True,
        "question": "se è domanda botanica, riportala qui altrimenti null"
    }

    user = f"""
Analizza questo messaggio e restituisci SOLO JSON valido, senza testo extra.
Schema esempio:
{json.dumps(instruction, ensure_ascii=False)}

Contesto (se presente):
- Riepilogo conversazione: {history_summary or "nessuno"}
- Ultimi lavori: {last_jobs or "nessuno"}

Messaggio utente: {user_message}
"""

    # Tentiamo JSON “pulito”
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=300,
    )
    text = completion.choices[0].message.content.strip()

    # Parse robusto: prova json diretto, altrimenti estrai blocco
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass

    # fallback semplice
    return {
        "intent": "mixed",
        "services": [],
        "details": None,
        "hours_hint": None,
        "wants_quote": True,
        "wants_booking": True,
        "question": None,
    }

def estimate_hours(service, user_message, details):
    """
    Stima ore con euristiche semplici (meglio deterministiche che inventate).
    Se l'utente indica ore, usiamo quelle.
    """
    # ore esplicite
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:h|ore|ora)\b", user_message.lower())
    if m:
        return float(m.group(1).replace(",", "."))

    text = (user_message + " " + (details or "")).lower()

    # metri per siepe
    m2 = re.search(r"(\d+)\s*metri", text)
    if service == "potatura siepi" and m2:
        meters = int(m2.group(1))
        # 40 m/h base
        return max(1.0, round(meters / 40.0, 1))

    # prato mq
    mq = re.search(r"(\d+)\s*(?:mq|m2)", text)
    if service == "taglio prato" and mq:
        area = int(mq.group(1))
        # 300 mq/h base
        return max(1.0, round(area / 300.0, 1))

    # default prudente
    return 2.0

def format_quote(service, hours):
    rate = SERVICES.get(service)
    if rate is None:
        return None
    total = round(rate * float(hours), 2)
    return rate, total

# =========================
# Risposta “specialistica” (con contesto + limiti)
# =========================
def ai_answer(user_message, history_summary=None, last_jobs=None):
    prompt = f"""
Messaggio cliente:
\"\"\"{user_message}\"\"\"

Contesto:
- Riepilogo conversazione: {history_summary or "nessuno"}
- Ultimi lavori: {last_jobs or "nessuno"}

Rispondi in modo chiaro, professionale e SOLO in ambito giardinaggio.
Se l'utente chiede info su piante, rispondi con consigli pratici.
Se serve un sopralluogo, dillo.
"""
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=500,
    )
    return completion.choices[0].message.content.strip()

# =========================
# WHATSAPP WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    phone = request.form.get("From")  # es: whatsapp:+39348...
    user_message = (request.form.get("Body", "") or "").strip()
    if not user_message:
        user_message = "Ciao"

    # carica sessione breve
    session = SESSIONS.get(phone, {
        "state": "idle",
        "service": None,
        "details": None,
        "hours": None,
        "quote_total": None,
        "slot_start": None,
        "slot_end": None
    })

    # memoria lunga (ultimi lavori)
    jobs_summary = last_jobs_summary(phone)

    # 1) Se siamo in attesa conferma, intercetta conferma / rifiuto
    if session.get("state") == "awaiting_confirmation":
        if user_message.lower() in CONFIRM_WORDS:
            # prova a creare evento
            if not session.get("slot_start") or not session.get("slot_end"):
                # se manca slot, ricostruisci e riproponi
                reply = "Perfetto. Prima di confermare, mi dici se preferisci mattina o pomeriggio nei prossimi giorni?"
            else:
                start_dt = dt.datetime.fromisoformat(session["slot_start"]).astimezone(TZ)
                end_dt = dt.datetime.fromisoformat(session["slot_end"]).astimezone(TZ)

                # crea evento (se google ok)
                event_id = None
                if calendar_service:
                    summary = f"{session.get('service', 'Lavoro giardino')} - Cliente WhatsApp"
                    desc = f"Cliente: {phone}\nDettagli: {session.get('details')}\nPreventivo indicativo: {session.get('quote_total')}€"
                    try:
                        event_id = create_calendar_event(summary, desc, start_dt, end_dt)
                    except Exception:
                        event_id = None

                # salva memoria lunga
                save_job(
                    phone=phone,
                    service=session.get("service"),
                    details=session.get("details"),
                    hours=float(session.get("hours") or 0),
                    quote_eur=float(session.get("quote_total") or 0),
                    event_id=event_id,
                    start_iso=start_dt.isoformat(),
                    end_iso=end_dt.isoformat(),
                )

                when_txt = start_dt.strftime("%d/%m/%Y %H:%M")
                reply = (
                    f"✅ Perfetto! Ho fissato l’appuntamento per **{session.get('service')}** "
                    f"({session.get('details') or 'dettagli da definire'})\n"
                    f"📅 **{when_txt}** (durata {session.get('hours')}h)\n"
                    f"💶 Preventivo indicativo: {session.get('quote_total')}€\n\n"
                    "Se vuoi aggiungere un indirizzo o dettagli (altezza siepe, accessi, foto), scrivimi pure."
                )

                # reset sessione breve (non perdere la lunga)
                session = {
                    "state": "idle",
                    "service": None,
                    "details": None,
                    "hours": None,
                    "quote_total": None,
                    "slot_start": None,
                    "slot_end": None
                }
                SESSIONS[phone] = session

                resp = MessagingResponse()
                resp.message(reply)
                return str(resp)

        # se non è conferma, trattalo come messaggio normale ma includendo contesto
        # (non resettiamo lo stato)
        # prosegue sotto

    # 2) Estrazione intent/dati
    extracted = ai_extract(user_message, history_summary=session.get("summary"), last_jobs=jobs_summary)

    # Semplifica: identifica servizio (primo valido)
    service = None
    for s in (extracted.get("services") or []):
        s_low = str(s).lower().strip()
        if s_low in SERVICES:
            service = s_low
            break

    # se non riconosce, prova match “fuzzy” semplice
    if not service:
        low = user_message.lower()
        for s in SERVICES:
            if s in low:
                service = s
                break
        # alias comuni
        if not service:
            if "siepe" in low:
                service = "potatura siepi"
            elif "prato" in low or "erba" in low:
                service = "taglio prato"
            elif "corda" in low:
                service = "potatura su corda"
            elif "albero" in low or "alberi" in low:
                service = "potatura alberi"

    # details
    details = extracted.get("details")
    if not details:
        # estrai metri/mq come fallback
        m = re.search(r"(\d+)\s*metri", user_message.lower())
        if m:
            details = f"siepe {m.group(1)} metri"
        mq = re.search(r"(\d+)\s*(?:mq|m2)", user_message.lower())
        if mq:
            details = (details + ", " if details else "") + f"prato {mq.group(1)} mq"

    # 3) Se è una domanda tecnica botanica/info → rispondi, ma se chiede anche preventivo/appuntamento continuiamo
    wants_quote = bool(extracted.get("wants_quote"))
    wants_booking = bool(extracted.get("wants_booking"))

    # se ha fatto solo una domanda info
    only_info = (extracted.get("intent") == "info") and not wants_quote and not wants_booking and not service
    if only_info:
        reply = ai_answer(user_message, history_summary=session.get("summary"), last_jobs=jobs_summary)
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # 4) Preventivo: se non abbiamo servizio → chiedi
    if (wants_quote or extracted.get("intent") in {"quote", "mixed"}) and not service:
        reply = (
            "Perfetto 😊 Per farti un preventivo mi dici quale servizio ti serve?\n"
            "Esempi: taglio prato, potatura siepi, potatura alberi, potatura su corda, pulizia giardino, raccolta foglie, smaltimento verde."
        )
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # 5) Calcolo ore e preventivo
    hours = None
    quote_total = None
    rate = None
    if service:
        hours = extracted.get("hours_hint") or estimate_hours(service, user_message, details)
        rate, quote_total = format_quote(service, hours)
        # salva in memoria breve
        session["service"] = service
        session["details"] = details
        session["hours"] = float(hours)
        session["quote_total"] = float(quote_total)

    # 6) Se vuole appuntamento (o se ha accettato implicitamente) → trova slot e proponi
    # Consideriamo booking se:
    # - wants_booking True
    # - oppure l'utente dice "prenota/ appuntamento/ quando sei libero"
    low = user_message.lower()
    if any(k in low for k in ["appunt", "prenot", "quando sei libero", "disponibil", "puoi venire"]) or wants_booking:
        if not service:
            reply = "Certo! Per fissare l’appuntamento mi dici quale lavoro dobbiamo fare? (es. potatura siepi, taglio prato)"
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        # se Google Calendar non configurato, fallback manuale
        if not calendar_service:
            reply = (
                f"Perfetto! Per **{service}** ({details or 'dettagli da definire'}) stimerei circa **{hours}h**.\n"
                f"Preventivo indicativo: **{quote_total}€**.\n\n"
                "📅 Per l’appuntamento: dimmi tu 2-3 disponibilità (giorno e fascia oraria) e confermo."
            )
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        # trova slot libero
        slot = find_first_slot(hours)
        if not slot:
            reply = (
                f"Per **{service}** ({details or 'dettagli da definire'}) stimerei **{hours}h**.\n"
                f"Preventivo indicativo: **{quote_total}€**.\n\n"
                "In questi giorni ho l’agenda piena 😅 Dimmi tu che giorno preferisci (mattina/pomeriggio) e provo a incastrarlo."
            )
            resp = MessagingResponse()
            resp.message(reply)
            return str(resp)

        start_dt, end_dt = slot
        when_txt = start_dt.strftime("%d/%m/%Y %H:%M")
        session["slot_start"] = start_dt.isoformat()
        session["slot_end"] = end_dt.isoformat()
        session["state"] = "awaiting_confirmation"
        SESSIONS[phone] = session

        reply = (
            f"Perfetto! Per **{service}** ({details or 'dettagli da definire'}) stimerei **{hours}h**.\n"
            f"💶 Preventivo indicativo: **{quote_total}€** ({rate}€/h)\n\n"
            f"📅 Ho trovato questo primo slot libero: **{when_txt}**.\n"
            "Vuoi che lo confermi e lo inserisca in agenda? (rispondi: **OK**)"
        )
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # 7) Se non chiede appuntamento, ma abbiamo fatto preventivo → proponi tu l’opzione
    if service and quote_total is not None:
        reply = (
            f"Per **{service}** ({details or 'dettagli da definire'}) stimerei **{hours}h**.\n"
            f"💶 Preventivo indicativo: **{quote_total}€** ({rate}€/h).\n\n"
            "Vuoi anche fissare un appuntamento? Se mi scrivi “quando sei libero” ti propongo il primo slot disponibile."
        )
        SESSIONS[phone] = session
        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    # 8) fallback: risposta informativa controllata
    reply = ai_answer(user_message, history_summary=session.get("summary"), last_jobs=jobs_summary)
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
