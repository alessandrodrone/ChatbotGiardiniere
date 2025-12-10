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
    "trattamenti antiparassitari": 20,
    "pulizia giardino": 20,
    "raccolta foglie": 15,
    "smaltimento verde": 15
}

# Date disponibili (esempio)
DISPONIBILITA = [
    "2025-12-10T10:00:00",
    "2025-12-10T14:00:00",
    "2025-12-11T09:00:00"
]

# Sessioni utenti per preventivi e appuntamenti
SESSIONS = {}
APPUNTAMENTI = []

# Thread per inviare promemoria
def promemoria_worker():
    while True:
        now = datetime.datetime.now()
        for appu in APPUNTAMENTI:
            dt_app = appu['datetime']
            numero = appu['numero']
            inviato = appu.get('reminder_sent', False)
            if not inviato and 0 <= (dt_app - now).total_seconds() <= 86400:  # 24h prima
                messaggio = f"Promemoria: il tuo appuntamento per {appu['servizio']} è domani alle {dt_app.strftime('%H:%M')}."
                twilio_client.messages.create(
                    body=messaggio,
                    from_=f'whatsapp:{TWILIO_WHATSAPP_NUMBER}',
                    to=numero
                )
                appu['reminder_sent'] = True
        time.sleep(3600)

threading.Thread(target=promemoria_worker, daemon=True).start()

def chiedi_gpt(prompt, max_tokens=300):
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens
    )
    return response.choices[0].message.content.strip()

def estrai_servizio(msg):
    prompt = f"Identifica se il seguente messaggio contiene uno dei servizi: {', '.join(SERVIZI.keys())}. " \
             f"Rispondi solo con il nome del servizio o 'nessuno'. Testo: {msg}"
    servizio = chiedi_gpt(prompt, max_tokens=20).lower()
    if servizio in SERVIZI:
        return servizio
    return None

@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get('From')
    msg = request.form.get('Body', '').strip()
    response = MessagingResponse()
    session = SESSIONS.get(from_number, {"step": 0})

    # Controlla se l'utente è in un flusso di preventivo/appuntamento
    if session["step"] in [1,2,3]:
        if session["step"] == 1:
            servizio = estrai_servizio(msg)
            if servizio:
                session["servizio"] = servizio
                reply = f"Perfetto! Quante ore pensi ci vorranno per {servizio}? (es. 2 o 3.5)"
                session["step"] = 2
            else:
                reply = "Non ho capito il servizio. Puoi scrivere ad esempio: potatura siepi, taglio prato, potatura su corda..."
        elif session["step"] == 2:
            try:
                ore = float(msg)
                session["ore"] = ore
                prezzo = SERVIZI[session['servizio']] * ore
                session["prezzo"] = prezzo
                reply = f"Il preventivo per {session['servizio']} è:\n\n👉 {ore}h × {SERVIZI[session['servizio']]} €/h = {prezzo}€\n"
                reply += "Vuoi anche prenotare un appuntamento? Rispondi con 'sì' o 'no'."
                session["step"] = 3
            except:
                reply = "Per favore indica le ore come numero (es. 2 o 3.5)."
        elif session["step"] == 3:
            if msg.lower() in ["sì","si","ok","yes"]:
                reply = "Ecco le date disponibili:\n"
                for i,d in enumerate(DISPONIBILITA):
                    dt = datetime.datetime.fromisoformat(d)
                    reply += f"{i+1}. {dt.strftime('%d/%m/%Y %H:%M')}\n"
                reply += "Rispondi con il numero della data che preferisci."
                session["step"] = 4
            else:
                reply = "Va bene, se vuoi un preventivo o un appuntamento scrivimi!"
                session["step"] = 0
        elif session["step"] == 4:
            try:
                scelta = int(msg)-1
                data_scelta = DISPONIBILITA[scelta]
                dt_start = datetime.datetime.fromisoformat(data_scelta)
                dt_end = dt_start + datetime.timedelta(hours=session["ore"])
                # Inserisci evento su Google Calendar
                event = {
                    'summary': f"{session['servizio']} - Cliente WhatsApp",
                    'start': {'dateTime': dt_start.isoformat(), 'timeZone': 'Europe/Rome'},
                    'end': {'dateTime': dt_end.isoformat(), 'timeZone': 'Europe/Rome'},
                }
                calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
                # Aggiungi promemoria
                APPUNTAMENTI.append({
                    'numero': from_number,
                    'servizio': session['servizio'],
                    'datetime': dt_start,
                    'reminder_sent': False
                })
                reply = f"Appuntamento confermato per {session['servizio']} il {dt_start.strftime('%d/%m/%Y %H:%M')} ✅"
                session["step"] = 0
            except:
                reply = "Non ho capito la scelta. Rispondi con il numero della data disponibile."
        SESSIONS[from_number] = session
        response.message(reply)
        return str(response)

    # Flusso generale: riconosce intent e risponde
    if any(k in msg.lower() for k in ["preventivo","prezzo","quanto costa"]):
        session["step"] = 1
        SESSIONS[from_number] = session
        response.message("Perfetto! Quale servizio ti interessa?")
        return str(response)
    elif any(k in msg.lower() for k in ["prenota","appuntamento","quando sei libero"]):
        session["step"] = 1
        SESSIONS[from_number] = session
        response.message("Perfetto! Quale servizio vuoi prenotare?")
        return str(response)
    else:
        # Risposta libera con GPT
        prompt = f"Sei un assistente di giardinaggio esperto. Rispondi in modo chiaro ed educato alla domanda: {msg}"
        reply = chiedi_gpt(prompt)
        response.message(reply)
        return str(response)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

