from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import openai
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
import datetime
import threading
import time

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

# Servizi e prezzi aggiornati
SERVIZI = {
    "taglio prato": 25,
    "potatura siepi": 30,
    "potatura alberi": 35,
    "potatura su corda": 60,
    "trattamenti antiparassitari": 20,
    "pulizia giardino": 20,
    "raccolta foglie": 15,
    "smaltimento verde": 15
}

# Lista appuntamenti
APPUNTAMENTI = []

# Funzione per trovare slot liberi
def trova_slot_libero(durata_ore):
    oggi = datetime.datetime.now()
    for i in range(14):  # controlla per i prossimi 14 giorni
        giorno = oggi + datetime.timedelta(days=i)
        for ora in [9, 11, 14, 16]:  # orari disponibili
            start = giorno.replace(hour=ora, minute=0, second=0, microsecond=0)
            end = start + datetime.timedelta(hours=durata_ore)
            occupato = False
            for appu in APPUNTAMENTI:
                if not (end <= appu['start'] or start >= appu['end']):
                    occupato = True
                    break
            if not occupato:
                return start, end
    return None, None

# Thread promemoria
def promemoria_worker():
    while True:
        now = datetime.datetime.now()
        for appu in APPUNTAMENTI:
            if not appu.get('reminder_sent') and 0 <= (appu['start'] - now).total_seconds() <= 86400:
                twilio_client.messages.create(
                    body=f"Promemoria: il tuo appuntamento per {appu['servizio']} è domani alle {appu['start'].strftime('%H:%M')}.",
                    from_=f'whatsapp:{TWILIO_WHATSAPP_NUMBER}',
                    to=appu['numero']
                )
                appu['reminder_sent'] = True
        time.sleep(3600)

threading.Thread(target=promemoria_worker, daemon=True).start()

@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get('From')
    msg = request.form.get('Body', '')

    # Chiamata a ChatGPT per capire intenzione e rispondere
    prompt = f"""
Sei un assistente intelligente di giardinaggio. 
Il cliente scrive: "{msg}"
Rispondi educatamente e chiaramente. 
Se il cliente chiede preventivi o appuntamenti, proponi servizi da questo elenco con prezzi orari:
{SERVIZI}
Se vuole prenotare, scegli automaticamente la prima data disponibile e comunica la conferma. 
Se fa domande generiche (es. quando tagliare il ginco biloba), rispondi correttamente.
Rispodi in italiano.
"""

    response_gpt = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300
    )

    reply = response_gpt.choices[0].message.content.strip()

    # Controllo se il GPT ha chiesto prenotazione
    for servizio in SERVIZI.keys():
        if servizio in msg.lower():
            # trova prima disponibilità
            match = [int(s) for s in msg.split() if s.replace('.','',1).isdigit()]
            ore = match[0] if match else 2
            start, end = trova_slot_libero(ore)
            if start:
                # Salva su Google Calendar
                event = {
                    'summary': f"{servizio} - Cliente WhatsApp",
                    'start': {'dateTime': start.isoformat(), 'timeZone': 'Europe/Rome'},
                    'end': {'dateTime': end.isoformat(), 'timeZone': 'Europe/Rome'},
                }
                calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                APPUNTAMENTI.append({
                    'numero': from_number,
                    'servizio': servizio,
                    'start': start,
                    'end': end,
                    'reminder_sent': False
                })
                reply += f"\n\nAppuntamento confermato per {servizio} il {start.strftime('%d/%m/%Y %H:%M')}."

    twilio_resp = MessagingResponse()
    twilio_resp.message(reply)
    return str(twilio_resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
