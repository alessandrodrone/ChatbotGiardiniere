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

# Servizi e prezzi
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

# Disponibilità esempio
DISPONIBILITA = [
    "2025-12-10T10:00:00",
    "2025-12-10T14:00:00",
    "2025-12-11T09:00:00"
]

SESSIONS = {}
APPUNTAMENTI = []

# Thread per promemoria 24h prima
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

def genera_risposta_gpt(prompt):
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300
    )
    return response.choices[0].message['content'].strip()

@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get('From')
    msg = request.form.get('Body', '').strip()
    session = SESSIONS.get(from_number, {"stato": "inizio"})
    response = MessagingResponse()

    # Comando di GPT per capire intent
    prompt = f"""
Sei un assistente di giardinaggio. Analizza questo messaggio del cliente e identifica:
1. Se vuole fare una domanda generale (rispondi con 'domanda')
2. Se vuole un preventivo (rispondi con 'preventivo')
3. Se vuole prenotare un appuntamento (rispondi con 'appuntamento')
Puoi anche combinare le azioni in uno stesso messaggio separandole con virgola.
Messaggio: {msg}
Rispondi solo con le parole chiave corrispondenti.
"""
    intent = genera_risposta_gpt(prompt).lower()
    
    reply = ""
    
    # Gestione dinamica
    if "domanda" in intent:
        reply += genera_risposta_gpt(f"Rispondi educatamente alla domanda di giardinaggio: {msg}\n")
    
    if "preventivo" in intent:
        # Proviamo a estrarre il servizio dal messaggio
        servizi_richiesti = []
        for s in SERVIZI.keys():
            if s in msg.lower():
                servizi_richiesti.append(s)
        if not servizi_richiesti:
            # Chiedi quale servizio
            reply += "Quale servizio ti interessa tra i seguenti?\n" + ", ".join(SERVIZI.keys())
            session['stato'] = "attesa_servizio"
        else:
            for s in servizi_richiesti:
                ore = 2  # default se non specificato
                prezzo = SERVIZI[s] * ore
                reply += f"Il preventivo per {s} è:\n👉 {ore}h × {SERVIZI[s]} €/h = {prezzo} €\n"
            reply += "Vuoi anche prenotare un appuntamento?"
            session['stato'] = "offerta_appuntamento"
    
    if "appuntamento" in intent:
        # Prenotazione automatica sulla prima disponibilità
        dt_str = DISPONIBILITA[0]
        dt_start = datetime.datetime.fromisoformat(dt_str)
        dt_end = dt_start + datetime.timedelta(hours=2)
        event = {
            'summary': f"Appuntamento WhatsApp",
            'start': {'dateTime': dt_start.isoformat(), 'timeZone': 'Europe/Rome'},
            'end': {'dateTime': dt_end.isoformat(), 'timeZone': 'Europe/Rome'},
        }
        calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        APPUNTAMENTI.append({
            'numero': from_number,
            'servizio': "Generico",
            'datetime': dt_start,
            'reminder_sent': False
        })
        reply += f"\nHo prenotato un appuntamento il {dt_start.strftime('%d/%m/%Y %H:%M')}."
        session['stato'] = "inizio"
    
    # Gestione caso attesa servizio
    if session.get('stato') == "attesa_servizio":
        for s in SERVIZI.keys():
            if s in msg.lower():
                ore = 2
                prezzo = SERVIZI[s] * ore
                reply = f"Il preventivo per {s} è:\n👉 {ore}h × {SERVIZI[s]} €/h = {prezzo} €\nVuoi anche prenotare un appuntamento?"
                session['stato'] = "offerta_appuntamento"
    
    SESSIONS[from_number] = session
    if not reply:
        reply = "Non ho capito, puoi ripetere per favore?"
    response.message(reply)
    return str(response)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
