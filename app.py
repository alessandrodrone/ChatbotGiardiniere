from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
import datetime
import threading
from twilio.rest import Client

app = Flask(__name__)

# --- Configurazione Twilio ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# --- Configurazione OpenAI ---
openai.api_key = os.getenv("OPENAI_API_KEY")

# --- Configurazione Google Calendar ---
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
    "trattamenti antiparassitari": 20,
    "pulizia giardino": 20,
    "raccolta foglie": 15,
    "smaltimento verde": 15
}

# --- Date disponibili di esempio ---
DISPONIBILITA = [
    "2025-12-10T10:00:00",
    "2025-12-10T14:00:00",
    "2025-12-11T09:00:00"
]

# --- Sessioni utenti ---
SESSIONS = {}
APPUNTAMENTI = []

# --- Funzione intelligente per ChatGPT ---
def gpt_rispondi(messaggio):
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": messaggio}],
        max_tokens=300
    )
    return response.choices[0].message.content.strip()

# --- Thread per promemoria ---
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
        threading.Event().wait(3600)

threading.Thread(target=promemoria_worker, daemon=True).start()

# --- Funzione per estrarre il servizio dal messaggio ---
def estrai_servizio(msg):
    for servizio in SERVIZI.keys():
        if servizio in msg.lower():
            return servizio
    return None

# --- Endpoint WhatsApp ---
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get('From')
    msg = request.form.get('Body', '')
    response = MessagingResponse()
    session = SESSIONS.get(from_number, {"step": 0})

    # Step 0: iniziale
    if session["step"] == 0:
        session["step"] = 1
        SESSIONS[from_number] = session
        response.message("Ciao! Vuoi ricevere un preventivo, prenotare un appuntamento o fare una domanda?")
        return str(response)

    # Step 1: capire cosa vuole
    scelta = msg.lower()
    if "preventivo" in scelta:
        session["step"] = 2
        SESSIONS[from_number] = session
        response.message("Perfetto! Quale servizio ti interessa?")
        return str(response)
    elif "prenotare" in scelta or "appuntamento" in scelta:
        session["step"] = 3
        SESSIONS[from_number] = session
        response.message("Quale servizio vuoi prenotare?")
        return str(response)
    else:
        # Risposta libera intelligente
        reply = gpt_rispondi(msg)
        response.message(reply)
        return str(response)

    # Step 2: preventivo
    if session["step"] == 2:
        servizio = estrai_servizio(msg)
        if servizio:
            session["servizio"] = servizio
            prezzo = SERVIZI[servizio]
            reply = f"Il prezzo orario per {servizio} è {prezzo} €/h. Quante ore pensi siano necessarie?"
            session["step"] = 4
        else:
            reply = "Non ho capito il servizio. Puoi scrivere esattamente il nome del servizio tra: " + ", ".join(SERVIZI.keys())
        SESSIONS[from_number] = session
        response.message(reply)
        return str(response)

    # Step 4: ricevo ore e calcolo preventivo
    if session["step"] == 4:
        try:
            ore = float(msg)
            session["ore"] = ore
            prezzo_totale = SERVIZI[session['servizio']] * ore
            reply = f"Il preventivo per {session['servizio']} è:\n👉 {ore}h × {SERVIZI[session['servizio']]} €/h = {prezzo_totale} €\nVuoi anche prenotare un appuntamento?"
            session["step"] = 3
        except:
            reply = "Inserisci un numero valido di ore."
        SESSIONS[from_number] = session
        response.message(reply)
        return str(response)

    # Step 3: prenotazione appuntamento
    if session["step"] == 3:
        servizio = estrai_servizio(msg)
        if servizio:
            session["servizio"] = servizio
            # Prende prima data disponibile
            dt_start = datetime.datetime.fromisoformat(DISPONIBILITA[0])
            dt_end = dt_start + datetime.timedelta(hours=1)
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
            reply = f"Appuntamento confermato per {session['servizio']} il {dt_start.strftime('%d/%m/%Y %H:%M')}."
            session["step"] = 0
        else:
            reply = "Non ho capito il servizio. Scrivilo esattamente tra: " + ", ".join(SERVIZI.keys())
        SESSIONS[from_number] = session
        response.message(reply)
        return str(response)

# --- Avvio app ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
