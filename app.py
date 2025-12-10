from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import openai
import os
import datetime
import threading
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# --- Twilio ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# --- OpenAI ---
openai.api_key = os.getenv("OPENAI_API_KEY")

# --- Google Calendar ---
SCOPES = ['https://www.googleapis.com/auth/calendar']
SERVICE_ACCOUNT_FILE = 'credentials.json'
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
calendar_service = build('calendar', 'v3', credentials=credentials)
CALENDAR_ID = 'primary'

# --- Servizi e prezzi ---
SERVIZI = {
    "taglio prato": 25,
    "potatura siepi": 30,
    "potatura alberi": 35,
    "potatura su corda": 60,
    "trattamenti antiparassitari": 30,
    "pulizia giardino": 25,
    "raccolta foglie": 20,
    "smaltimento verde": 20
}

DISPONIBILITA = [
    "2025-12-10T10:00:00",
    "2025-12-10T14:00:00",
    "2025-12-11T09:00:00"
]

SESSIONS = {}
APPUNTAMENTI = []

# --- Thread promemoria ---
def promemoria_worker():
    while True:
        now = datetime.datetime.now()
        for appu in APPUNTAMENTI:
            dt_app = appu['datetime']
            numero = appu['numero']
            if not appu.get('reminder_sent', False):
                if 0 <= (dt_app - now).total_seconds() <= 86400:
                    messaggio = f"Promemoria: appuntamento per {appu['servizio']} domani alle {dt_app.strftime('%H:%M')}."
                    twilio_client.messages.create(
                        body=messaggio,
                        from_=f'whatsapp:{TWILIO_WHATSAPP_NUMBER}',
                        to=numero
                    )
                    appu['reminder_sent'] = True
        threading.Event().wait(3600)

threading.Thread(target=promemoria_worker, daemon=True).start()

# --- Funzione principale ---
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get('From')
    msg = request.form.get('Body', '')
    response = MessagingResponse()
    session = SESSIONS.get(from_number, {"step": 0})

    # Identifica se il messaggio riguarda un servizio
    servizio = None
    for s in SERVIZI.keys():
        if s in msg.lower():
            servizio = s
            break

    if session["step"] == 0 and servizio:
        session["servizio"] = servizio
        reply = f"Perfetto! Quante ore pensi per {servizio}? (es. 2 o 3.5)"
        session["step"] = 1

    elif session["step"] == 1:
        match = re.search(r"\d+(\.\d+)?", msg)
        if match:
            ore = float(match.group())
            session["ore"] = ore
            prezzo = SERVIZI[session['servizio']] * ore
            session["prezzo"] = prezzo
            reply = f"Preventivo per {session['servizio']}: {ore}h × {SERVIZI[session['servizio']]} €/h = {prezzo} €\n"
            reply += "Ecco le date disponibili:\n"
            for i, d in enumerate(DISPONIBILITA):
                dt = datetime.datetime.fromisoformat(d)
                reply += f"{i+1}. {dt.strftime('%d/%m/%Y %H:%M')}\n"
            reply += "Rispondi con il numero della data che preferisci."
            session["step"] = 2
        else:
            reply = "Per favore indica le ore in numero (es. 2 o 3.5)."

    elif session["step"] == 2:
        try:
            scelta = int(msg) - 1
            dt_start = datetime.datetime.fromisoformat(DISPONIBILITA[scelta])
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
            reply = f"Appuntamento confermato per {session['servizio']} il {dt_start.strftime('%d/%m/%Y %H:%M')}. ✅"
            session["step"] = 0
        except:
            reply = "Non ho capito la scelta. Rispondi con il numero della data disponibile."

    else:
        # Risponde a qualsiasi altra domanda con GPT
        gpt_resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": msg}],
            max_tokens=300
        )
        reply = gpt_resp.choices[0].message.content
        session["step"] = 0

    SESSIONS[from_number] = session
    response.message(reply)
    return str(response)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
