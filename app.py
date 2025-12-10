from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import openai
import os
import datetime
import threading
import time
from google.oauth2 import service_account
from googleapiclient.discovery import build
import requests

app = Flask(__name__)

# -------------------- Config --------------------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

openai.api_key = os.getenv("OPENAI_API_KEY")
GOOGLE_MAPS_KEY = os.getenv("GOOGLE_MAPS_KEY")  # Chiave API per Distance Matrix

SCOPES = ['https://www.googleapis.com/auth/calendar']
SERVICE_ACCOUNT_FILE = 'credentials.json'
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
calendar_service = build('calendar', 'v3', credentials=credentials)
CALENDAR_ID = 'primary'

# -------------------- Servizi e prezzi --------------------
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

# Slot disponibili (esempio)
DISPONIBILITA = [
    "2025-12-10T10:00:00",
    "2025-12-10T14:00:00",
    "2025-12-11T09:00:00"
]

# Punto di riferimento per distanza
BASE_LOCATION = "Selvazzano Dentro, Padova, Italy"

# Stato clienti e appuntamenti
SESSIONS = {}
APPUNTAMENTI = []

# -------------------- Thread Promemoria --------------------
def promemoria_worker():
    while True:
        now = datetime.datetime.now()
        for appu in APPUNTAMENTI:
            dt_app = appu['datetime']
            numero = appu['numero']
            if not appu.get('reminder_sent', False) and 0 <= (dt_app - now).total_seconds() <= 86400:
                messaggio = f"Promemoria: il tuo appuntamento per {appu['servizio']} è domani alle {dt_app.strftime('%H:%M')}."
                twilio_client.messages.create(
                    body=messaggio,
                    from_=f'whatsapp:{TWILIO_WHATSAPP_NUMBER}',
                    to=numero
                )
                appu['reminder_sent'] = True
        time.sleep(3600)

threading.Thread(target=promemoria_worker, daemon=True).start()

# -------------------- Funzioni --------------------
def estrai_servizio(msg):
    prompt = f"""
    Identifica uno dei seguenti servizi nel testo: {', '.join(SERVIZI.keys())}.
    Rispondi solo con il nome del servizio o 'nessuno'.
    Testo: {msg}
    """
    response = openai.Completion.create(
        model="text-davinci-003",
        prompt=prompt,
        max_tokens=20
    )
    servizio = response.choices[0].text.strip().lower()
    if servizio in SERVIZI:
        return servizio
    return None

def stima_durata(servizio):
    prompt = f"Sei un giardiniere esperto. Stima in ore il tempo medio per completare '{servizio}'. Rispondi solo con un numero decimale."
    response = openai.Completion.create(
        model="text-davinci-003",
        prompt=prompt,
        max_tokens=5
    )
    try:
        ore = float(response.choices[0].text.strip())
        return max(1, ore)
    except:
        return 2

def distanza_da_base(indirizzo_dest):
    url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins={BASE_LOCATION}&destinations={indirizzo_dest}&key={GOOGLE_MAPS_KEY}"
    r = requests.get(url).json()
    try:
        distanza = r['rows'][0]['elements'][0]['distance']['value']  # metri
        return distanza
    except:
        return 10000  # se fallisce, assume 10 km

def scegli_slot_libero_ottimizzato(ore, indirizzo_cliente=None):
    ora_corrente = datetime.datetime.now()
    slot_disponibili = []
    for s in DISPONIBILITA:
        dt_slot = datetime.datetime.fromisoformat(s)
        if dt_slot >= ora_corrente + datetime.timedelta(minutes=30):
            overlap = False
            for a in APPUNTAMENTI:
                a_start = a['datetime']
                a_end = a_start + datetime.timedelta(hours=a['ore'])
                dt_end = dt_slot + datetime.timedelta(hours=ore)
                if (dt_slot < a_end and dt_end > a_start):
                    overlap = True
                    break
            if not overlap:
                slot_disponibili.append(dt_slot)
    
    if indirizzo_cliente and slot_disponibili:
        # Ordina gli slot in base alla distanza (approssimata)
        slot_disponibili.sort(key=lambda dt: distanza_da_base(indirizzo_cliente))
    
    return slot_disponibili[0] if slot_disponibili else None

# -------------------- Endpoint WhatsApp --------------------
@app.route("/bot", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get('From')
    msg = request.form.get('Body', '').lower()
    response = MessagingResponse()
    
    session = SESSIONS.get(from_number, {"mode": "idle"})
    
    servizio = estrai_servizio(msg)
    
    if servizio:
        ore = stima_durata(servizio)
        prezzo = SERVIZI[servizio] * ore
        
        # Inserisci l'indirizzo del cliente se disponibile nel messaggio
        indirizzo_cliente = None  # eventualmente estrarre dall'utente
        dt_slot = scegli_slot_libero_ottimizzato(ore, indirizzo_cliente)
        
        if dt_slot:
            dt_end = dt_slot + datetime.timedelta(hours=ore)
            event = {
                'summary': f"{servizio} - Cliente WhatsApp",
                'start': {'dateTime': dt_slot.isoformat(), 'timeZone': 'Europe/Rome'},
                'end': {'dateTime': dt_end.isoformat(), 'timeZone': 'Europe/Rome'},
            }
            calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
            
            APPUNTAMENTI.append({
                'numero': from_number,
                'servizio': servizio,
                'datetime': dt_slot,
                'ore': ore,
                'reminder_sent': False
            })
            
            reply = f"Ho prenotato automaticamente il servizio '{servizio}' per te.\nOrario: {dt_slot.strftime('%d/%m/%Y %H:%M')}\nDurata stimata: {ore}h\nPrezzo: {prezzo}€ ✅"
        else:
            reply = "Mi dispiace, non ci sono slot liberi al momento. Ti ricontatterò appena possibile."
    else:
        prompt = f"Sei un giardiniere esperto a Padova. Rispondi educatamente al seguente messaggio: {msg}"
        gpt_response = openai.Completion.create(
            model="text-davinci-003",
            prompt=prompt,
            max_tokens=250
        )
        reply = gpt_response.choices[0].text.strip()
    
    SESSIONS[from_number] = session
    response.message(reply)
    return str(response)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

