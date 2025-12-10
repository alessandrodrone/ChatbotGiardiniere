# app.py
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import openai
import os
import datetime
import time
import re
import threading
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------- CONFIG ----------
app = Flask(__name__)

# Environment variables (da impostare su Railway)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
TRAVEL_RATE_PER_KM = float(os.getenv("TRAVEL_RATE_PER_KM", "1.0"))
WORK_START = int(os.getenv("WORK_START", "8"))
WORK_END = int(os.getenv("WORK_END", "18"))
BASE_LOCATION = os.getenv("BASE_LOCATION", "Selvazzano Dentro, PD")
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")  # o id specifico

# Twilio + OpenAI init
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
openai.api_key = OPENAI_API_KEY

# Google Calendar setup (service account)
SCOPES = ['https://www.googleapis.com/auth/calendar']
SERVICE_ACCOUNT_FILE = 'credentials.json'  # must be in repo root
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
calendar_service = build('calendar', 'v3', credentials=credentials)

# ---------- BUSINESS DATA ----------
SERVIZI = {
    "taglio prato": 25.0,
    "potatura siepi": 30.0,
    "potatura alberi": 35.0,
    "potatura su corda": 60.0,
    "trattamenti antiparassitari": 40.0,
    "pulizia giardino": 28.0,
    "raccolta foglie": 22.0,
    "smaltimento verde": 30.0
}

SESSIONS = {}    # memoria in RAM (per test; per produzione usa DB)
APPUNTAMENTI = []  # lista in RAM per promemoria (puoi sincronizzare dal calendar)

# ---------- HELPERS ----------

def chatgpt_extract_intent(message):
    """
    Usa ChatGPT per estrarre intent strutturato dal messaggio.
    Ritorna dict con: intent (info|preventivo|prenotazione), servizio, ore, indirizzo, preferenze_data (iso), asap(bool)
    """
    system = (
        "You are an assistant that extracts structured booking info from user messages in Italian. "
        "Return a JSON only with keys: intent, servizio, ore, indirizzo, preferita, asap. "
        "Values: intent in [info, preventivo, prenotazione, altro]; servizio string or null; ore number or null; "
        "indirizzo string or null; preferita ISO date/time string or null; asap true/false."
    )
    prompt = f"Testo: {message}\nReturn JSON."

    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # se non disponibile, sostituisci con "gpt-4" o "gpt-3.5-turbo"
            messages=[
                {"role":"system","content":system},
                {"role":"user","content":prompt}
            ],
            max_tokens=200,
            temperature=0
        )
        text = resp['choices'][0]['message']['content'].strip()
        # tenta parse JSON
        import json
        data = json.loads(text)
        return data
    except Exception as e:
        # fallback semplice
        intent = "altro"
        servizio = None
        ore = None
        indirizzo = None
        preferita = None
        asap = False
        # keyword heuristics
        low = message.lower()
        if any(w in low for w in ["preventivo","quanto","prezzo","costo"]):
            intent = "preventivo"
        if any(w in low for w in ["prenot","appuntament","vieni","quando puoi"]):
            intent = "prenotazione"
        if any(w in low for w in ["quando","come","consiglio","informazione","info"]):
            intent = "info"
        # servizio heuristic
        for s in SERVIZI.keys():
            if s in low:
                servizio = s
                break
        # ore heuristic
        m = re.search(r"(\d+(\.\d+)?)\s*(ore|h)", low)
        if m:
            ore = float(m.group(1))
        # address heuristic (very simple: look for 'Padova' or street number)
        if "padova" in low or "selvazzano" in low or re.search(r"\d{4,}", low):
            indirizzo = None  # ask later
        return {"intent": intent, "servizio": servizio, "ore": ore, "indirizzo": indirizzo, "preferita": preferita, "asap": asap}

def get_distance_and_duration(origin, destination):
    """
    Usa Google Maps Distance Matrix per ottenere distanza (km) e durata (minutes).
    origin/destination: string address
    """
    if not GOOGLE_MAPS_API_KEY:
        return {"distance_km": 0.0, "duration_min": 0}
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": origin,
        "destinations": destination,
        "key": GOOGLE_MAPS_API_KEY,
        "units": "metric"
    }
    r = requests.get(url, params=params, timeout=10)
    data = r.json()
    try:
        el = data["rows"][0]["elements"][0]
        if el.get("status") == "OK":
            dist_m = el["distance"]["value"]
            dur_s = el["duration"]["value"]
            return {"distance_km": round(dist_m/1000.0, 2), "duration_min": int(dur_s/60)}
    except Exception:
        pass
    return {"distance_km": 0.0, "duration_min": 0}

def find_free_slot(duration_hours, travel_minutes, preferred_iso=None):
    """
    Cerca nel Google Calendar il primo slot libero compatibile con WORK_START-WORK_END
    Restituisce start datetime (aware naive) o None.
    """
    now = datetime.datetime.utcnow()
    # ricerca finestra 14 giorni
    time_min = now.isoformat() + "Z"
    time_max = (now + datetime.timedelta(days=14)).isoformat() + "Z"

    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "timeZone": "Europe/Rome",
        "items": [{"id": CALENDAR_ID}]
    }
    fb = calendar_service.freebusy().query(body=body).execute()
    busy = fb.get('calendars', {}).get(CALENDAR_ID, {}).get('busy', [])

    # genera candidate slots per ogni giorno dalle WORK_START alle WORK_END
    for day_offset in range(0, 14):
        day = (datetime.datetime.now() + datetime.timedelta(days=day_offset)).date()
        if preferred_iso:
            try:
                pref_dt = datetime.datetime.fromisoformat(preferred_iso)
                if pref_dt.date() != day:
                    continue
            except:
                pass
        # only weekdays unless user allows weekends (we allow all days but bound by hours)
        start_dt = datetime.datetime.combine(day, datetime.time(hour=WORK_START))
        end_dt = datetime.datetime.combine(day, datetime.time(hour=WORK_END))
        cursor = start_dt
        while cursor + datetime.timedelta(hours=duration_hours) <= end_dt:
            candidate_start = cursor
            candidate_end = candidate_start + datetime.timedelta(hours=duration_hours)
            # include travel buffer before job: travel_minutes maybe added to start or end depending; we block travel before
            blocked_start = candidate_start - datetime.timedelta(minutes=travel_minutes)
            blocked_end = candidate_end + datetime.timedelta(minutes=travel_minutes)
            # check overlap with busy
            overlap = False
            for b in busy:
                b_start = datetime.datetime.fromisoformat(b['start'].replace('Z',''))
                b_end = datetime.datetime.fromisoformat(b['end'].replace('Z',''))
                if not (blocked_end <= b_start or blocked_start >= b_end):
                    overlap = True
                    break
            if not overlap and candidate_start > datetime.datetime.now():
                return candidate_start
            cursor += datetime.timedelta(minutes=30)  # step 30min
    return None

def create_calendar_event(service_name, client_name, client_number, start_dt, duration_hours, address):
    """
    Inserisce evento su Google Calendar.
    """
    end_dt = start_dt + datetime.timedelta(hours=duration_hours)
    event = {
        'summary': f"{service_name} - {client_name or 'Cliente WhatsApp'}",
        'location': address or "",
        'description': f"Prenotazione via WhatsApp per {service_name}. Contatto: {client_number}",
        'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'Europe/Rome'},
        'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'Europe/Rome'},
        'reminders': {'useDefault': False, 'overrides': [{'method':'popup','minutes':1440}]}
    }
    ev = calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    return ev

def send_whatsapp(to, body):
    twilio_client.messages.create(
        body=body,
        from_=TWILIO_WHATSAPP_NUMBER,
        to=to
    )

# ---------- PROMEMORIA THREAD ----------
def reminders_worker():
    while True:
        now = datetime.datetime.now()
        # carica eventi futuri dal calendar e invia promemoria 24h prima via Twilio se non inviato
        # (qui per semplicità usiamo APPUNTAMENTI in memoria)
        for ap in APPUNTAMENTI:
            dt = ap['datetime']
            if not ap.get('reminder_sent') and 0 <= (dt - now).total_seconds() <= 86400:
                msg = f"Promemoria: domani alle {dt.strftime('%d/%m/%Y %H:%M')} abbiamo l'appuntamento per {ap['servizio']}."
                try:
                    send_whatsapp(ap['numero'], msg)
                    ap['reminder_sent'] = True
                except:
                    pass
        time.sleep(1800)

threading.Thread(target=reminders_worker, daemon=True).start()

# ---------- ROUTE ----------

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    from_number = request.form.get('From')  # es: whatsapp:+39...
    raw = request.form.get('Body', '')
    msg = raw.strip()
    resp = MessagingResponse()

    # user-friendly remove 'whatsapp:' prefix for storage
    user_phone = from_number

    session = SESSIONS.get(user_phone, {"step": 0})

    # Interpret message using ChatGPT extraction
    parsed = chatgpt_extract_intent(msg)
    intent = parsed.get("intent", "altro")
    servizio = parsed.get("servizio")
    ore = parsed.get("ore")
    indirizzo = parsed.get("indirizzo")
    preferita = parsed.get("preferita")
    asap = parsed.get("asap", False)

    # If user only asks info -> answer directly via GPT
    if intent == "info":
        # ask GPT for a friendly gardening answer in Italian
        try:
            g = openai.ChatCompletion.create(
                model="gpt-4o-mini",  # fallback if not available replace with gpt-4 or gpt-3.5-turbo
                messages=[
                    {"role":"system","content":"Sei un esperto giardiniere italiano. Rispondi brevemente e in modo pratico."},
                    {"role":"user","content": msg}
                ],
                max_tokens=300,
                temperature=0.2
            )
            answer = g['choices'][0]['message']['content'].strip()
        except:
            answer = "Ti posso aiutare con informazioni su taglio, potature e preventivi. Puoi dirmi esattamente cosa vuoi sapere?"
        resp.message(answer)
        return str(resp)

    # If user wants a preventivo
    if intent == "preventivo" or (servizio and intent != "prenotazione"):
        # ensure we have a service and hours
        if not servizio:
            # try GPT to map to known service
            servizio = None
            low = msg.lower()
            for s in SERVIZI.keys():
                if s in low:
                    servizio = s
                    break
        if not servizio:
            resp.message("Per preparare un preventivo dimmi quale servizio desideri (es. potatura alberi, potatura su corda, taglio prato...).")
            return str(resp)

        if not ore:
            resp.message(f"Quante ore circa pensi saranno necessarie per {servizio}? (es. 2 oppure 3.5).")
            # keep session expecting hours
            SESSIONS[user_phone] = {"step": "await_hours", "servizio": servizio}
            return str(resp)

        # calculate travel (if address provided)
        distance_info = {"distance_km":0.0, "duration_min":0}
        if indirizzo:
            distance_info = get_distance_and_duration(BASE_LOCATION, indirizzo)
        travel_cost = round(distance_info["distance_km"] * TRAVEL_RATE_PER_KM,2)
        base_price = SERVIZI.get(servizio, 30.0) * float(ore)
        total = round(base_price + travel_cost, 2)
        reply = (f"Preventivo stimato per {servizio} — {ore}h:\n"
                 f"Prezzo lavoro: {base_price:.2f}€\n"
                 f"Supplemento viaggio ({distance_info['distance_km']} km): {travel_cost:.2f}€\n"
                 f"Totale stimato: {total:.2f}€\n\nVuoi prenotare questo intervento? (sì/no)")
        resp.message(reply)
        # store session
        SESSIONS[user_phone] = {"step":"confirm_booking", "servizio":servizio, "ore":ore, "indirizzo":indirizzo, "price":total}
        return str(resp)

    # If user asked to book (prenotazione)
    if intent == "prenotazione" or session.get("step") in ["confirm_booking", "await_hours", "await_address"]:
        # handle multi-step session
        step = session.get("step", 0)
        if step == "await_hours":
            m = re.search(r"(\d+(\.\d+)?)", msg)
            if m:
                ore = float(m.group(1))
                session["ore"] = ore
                session["servizio"] = session.get("servizio")
                # continue to calculate price & propose slot
            else:
                resp.message("Non ho capito le ore. Indica un numero (es. 2 o 3.5).")
                SESSIONS[user_phone] = session
                return str(resp)

        if step == "confirm_booking":
            if msg.lower().startswith("s"):
                # proceed to schedule
                servizio = session["servizio"]
                ore = session["ore"]
                indirizzo = session.get("indirizzo") or indirizzo
            else:
                resp.message("Ok, preventivo annullato. Se vuoi puoi chiedere un altro preventivo.")
                SESSIONS[user_phone] = {"step":0}
                return str(resp)

        # Ensure we have indirizzo; if not, ask for it
        if not indirizzo:
            resp.message("Perfavore indicami l'indirizzo di lavoro (via, numero, città) così calcolo il tempo di viaggio e ti propongo uno slot.")
            SESSIONS[user_phone] = {"step":"await_address", "servizio":session.get("servizio"), "ore":session.get("ore")}
            return str(resp)

        # calculate travel info
        dist = get_distance_and_duration(BASE_LOCATION, indirizzo)
        travel_minutes = dist["duration_min"]
        # find free slot considering travel time
        chosen_start = find_free_slot(duration_hours=float(ore), travel_minutes=travel_minutes, preferred_iso=preferita)
        if not chosen_start:
            resp.message("Al momento non trovo slot liberi nei prossimi 14 giorni. Vuoi che ti proponga almeno delle date prossime?")
            return str(resp)

        # create event
        # client name heuristic: we don't have name => use phone
        client_name = None
        ev = create_calendar_event(session.get("servizio") or servizio, client_name, user_phone, chosen_start, float(ore), indirizzo)
        # store appointment for reminders
        APPUNTAMENTI.append({'numero': user_phone, 'servizio': session.get("servizio") or servizio, 'datetime': chosen_start, 'reminder_sent': False})
        # compute price again
        travel_cost = round(dist["distance_km"] * TRAVEL_RATE_PER_KM,2)
        base_price = SERVIZI.get(session.get("servizio") or servizio, 30.0) * float(ore)
        total = round(base_price + travel_cost, 2)

        reply = (f"Appuntamento prenotato il {chosen_start.strftime('%d/%m/%Y %H:%M')} per {session.get('servizio') or servizio}.\n"
                 f"Totale stimato: {total:.2f}€ (viaggio: {dist['distance_km']} km -> {travel_cost:.2f}€).\n"
                 f"Ho aggiunto l'appuntamento al calendario. Ti mando promemoria 24h prima.")
        resp.message(reply)
        SESSIONS[user_phone] = {"step":0}
        return str(resp)

    # fallback: general chat answer
    try:
        g = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"Sei un assistente di giardinaggio italiano, educato e utile."},
                {"role":"user","content": msg}
            ],
            max_tokens=300,
            temperature=0.2
        )
        answer = g['choices'][0]['message']['content'].strip()
    except:
        answer = "Scusa, non ho capito. Puoi ripetere o scrivermi 'preventivo' o 'prenota'?"
    resp.message(answer)
    return str(resp)

# run with gunicorn; do not include app.run() for Railway
