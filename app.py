from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
import datetime
import re
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
    "potatura siepi": 20,
    "taglio prato": 15,
    "piantumazione": 25
}

# Date disponibili (esempio)
DISPONIBILITA = [
    "2025-12-10T10:00:00",
    "2025-12-10T14:00:00",
    "2025-12-11T09:00:00"
]

# Sessioni clienti
SESSIONS = {}
APPUNTAMENTI = []

# Funzione per estrarre servizio dal messaggio usando GPT
def estrai_servizio(msg):
    prompt = f"Identifica uno dei seguenti servizi nel testo: {', '.join(SERVIZI.keys())}.\nTesto: {msg}\nRispondi solo con il nome del servizio o 'nessuno'."
    response = openai.Completion.create(
        model="text-davinci-003",
        prompt=prompt,
        max_tokens=20
    )
    servizio = response.choices[0].text.strip().lower()
    if servizio in SERVIZI:
        return servizio
    return None

# Thread per promemoria
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

@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get('From')
    msg = request.form.get('Body', '').lower()
    response = MessagingResponse()
    session = SESSIONS.get(from_number, {"step": 0})

    if session["step"] == 0:
        reply = "Ciao! Vuoi ricevere un preventivo o prenotare un lavoro? 📅"
        session["step"] = 1

    elif session["step"] == 1:
        servizio = estrai_servizio(msg)
        if servizio:
            session["servizio"] = servizio
            reply = f"Perfetto! Quante ore pensi ci vorranno per {servizio}? (es. 2 o 3.5)"
            session["step"] = 2
        else:
            reply = "Non ho capito il servizio. Puoi scrivere ad esempio: potatura siepi, taglio prato, piantumazione."

    elif session["step"] == 2:
        match = re.search(r"\d+(\.\d+)?", msg)
        if match:
            ore = float(match.group())
            session["ore"] = ore
            prezzo = SERVIZI[session['servizio']] * ore
            session["prezzo"] = prezzo
            reply = f"Il preventivo stimato per {session['servizio']} per {ore} ore è di {prezzo}€. Ecco le date disponibili:\n"
            for i, d in enumerate(DISPONIBILITA):
                dt = datetime.datetime.fromisoformat(d)
                reply += f"{i+1}. {dt.strftime('%d/%m/%Y %H:%M')}\n"
            reply += "Rispondi con il numero della data che preferisci."
            session["step"] = 3
        else:
            reply = "Per favore indica le ore in numero (es. 2 o 3.5)."

    elif session["step"] == 3:
        try:
            scelta = int(msg) - 1
            data_scelta = DISPONIBILITA[scelta]
            dt_start = datetime.datetime.fromisoformat(data_scelta)
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
            reply = f"Appuntamento confermato per {session['servizio']} il {dt_start.strftime('%d/%m/%Y %H:%M')}.\nGrazie! ✅"
            session["step"] = 0
        except:
            reply = "Non ho capito la scelta. Rispondi con il numero della data disponibile."

    else:
        gpt_response = openai.Completion.create(
            model="text-davinci-003",
            prompt=f"Sei un assistente di giardinaggio. Rispondi educatamente al messaggio: {msg}",
            max_tokens=150
        )
        reply = gpt_response.choices[0].text.strip()
        session["step"] = 0

    SESSIONS[from_number] = session
    response.message(reply)
    return str(response)

