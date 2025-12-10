from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
import datetime
import threading
import time
from twilio.rest import Client

app = Flask(__name__)

# Twilio setup
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# OpenAI setup
openai.api_key = os.getenv("OPENAI_API_KEY")

# Google Calendar setup
SCOPES = ['https://www.googleapis.com/auth/calendar']
SERVICE_ACCOUNT_FILE = 'credentials.json'
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
calendar_service = build('calendar', 'v3', credentials=credentials)
CALENDAR_ID = 'primary'

# Servizi e prezzi
SERVIZI = {
    "taglio prato": 25,
    "potatura siepi": 30,
    "potatura alberi": 35,
    "potatura su corda": 60,
    "trattamenti antiparassitari": 40,
    "pulizia giardino": 20,
    "raccolta foglie": 20,
    "smaltimento verde": 25
}

# Disponibilità (esempio)
DISPONIBILITA = [
    "2025-12-10T10:00:00",
    "2025-12-10T14:00:00",
    "2025-12-11T09:00:00"
]

# Sessioni clienti
SESSIONS = {}
APPUNTAMENTI = []

# Thread promemoria
def promemoria_worker():
    while True:
        now = datetime.datetime.now()
        for appu in APPUNTAMENTI:
            dt_app = appu['datetime']
            numero = appu['numero']
            inviato = appu.get('reminder_sent', False)
            if not inviato and 0 <= (dt_app - now).total_seconds() <= 86400:
                messaggio = f"Promemoria: il tuo appuntamento per {appu['servizio']} è domani alle {dt_app.strftime('%H:%M')}."
                twilio_client.messages.create(
                    body=messaggio,
                    from_=f'whatsapp:{TWILIO_WHATSAPP_NUMBER}',
                    to=numero
                )
                appu['reminder_sent'] = True
        time.sleep(3600)

threading.Thread(target=promemoria_worker, daemon=True).start()

# Funzione per GPT
def ask_gpt(prompt):
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300
    )
    return response.choices[0].message.content.strip()

# Funzione per capire il servizio richiesto
def estrai_servizio(msg):
    prompt = f"Identifica se il messaggio dell'utente richiede uno dei seguenti servizi: {', '.join(SERVIZI.keys())}. Rispondi solo con il nome del servizio o 'nessuno'. Messaggio: {msg}"
    servizio = ask_gpt(prompt).lower()
    if servizio in SERVIZI:
        return servizio
    return None

@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get('From')
    msg = request.form.get('Body', '').lower()
    response = MessagingResponse()
    session = SESSIONS.get(from_number, {"step": 0})

    try:
        if session["step"] == 0:
            servizio = estrai_servizio(msg)
            if servizio:
                session["servizio"] = servizio
                reply = f"Perfetto! Quante ore pensi ci vorranno per {servizio}? (es. 2 o 3.5)"
                session["step"] = 1
            else:
                reply = ask_gpt(f"Sei un assistente di giardinaggio. Rispondi educatamente al seguente messaggio: {msg}")

        elif session["step"] == 1:
            import re
            match = re.search(r"\d+(\.\d+)?", msg)
            if match:
                ore = float(match.group())
                session["ore"] = ore
                prezzo = SERVIZI[session['servizio']] * ore
                session["prezzo"] = prezzo
                reply = f"Il preventivo per {session['servizio']} è:\n\n👉 {ore}h × {SERVIZI[session['servizio']]} €/h = {prezzo} €\n\nVuoi anche prenotare un appuntamento?"
                session["step"] = 2
            else:
                reply = "Per favore indica le ore come numero (es. 2 o 3.5)."

        elif session["step"] == 2:
            if "si" in msg:
                # Prenota la prima disponibilità libera
                dt_scelta = DISPONIBILITA[0]
                dt_start = datetime.datetime.fromisoformat(dt_scelta)
                dt_end = dt_start + datetime.timedelta(hours=session["ore"])
                event = {
                    'summary': f"{session['servizio']} - Cliente WhatsApp",
                    'start': {'dateTime': dt_start.isoformat(), 'timeZone': 'Europe/Rome'},
                    'end': {'dateTime': dt_end.isoformat(), 'timeZone': 'Europe/Rome'},
                }
                calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                APPUNTAMENTI.append({
                    'numero': from_number,
                    'servizio': session['servizio'],
                    'datetime': dt_start,
                    'reminder_sent': False
                })
                reply = f"Appuntamento confermato per {session['servizio']} il {dt_start.strftime('%d/%m/%Y %H:%M')} ✅"
                session["step"] = 0
            else:
                reply = "Ok, se vuoi puoi chiedermi un preventivo o fare domande sul giardinaggio."
                session["step"] = 0

    except Exception as e:
        reply = f"Errore: {str(e)}"

    SESSIONS[from_number] = session
    response.message(reply)
    return str(response)
