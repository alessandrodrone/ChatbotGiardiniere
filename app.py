from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import openai
import os
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

openai.api_key = os.getenv("OPENAI_API_KEY")

SESSIONS = {}

SERVICES = {
    "taglio prato": 25,
    "potatura siepi": 30,
    "potatura alberi": 35,
    "potatura su corda": 60,
}

# GOOGLE CALENDAR
SCOPES = ["https://www.googleapis.com/auth/calendar"]
SERVICE_ACCOUNT_FILE = "credentials.json"

calendar_service = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
calendar_api = build("calendar", "v3", credentials=calendar_service)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")


@app.route("/bot", methods=["POST"])
def bot():
    from_number = request.form.get("From")
    msg = request.form.get("Body", "").lower().strip()

    session = SESSIONS.get(from_number, {"step": 0, "service": None, "hours": None})

    # -----------------------------------------------------
    # 1️⃣ DOMANDE NORMALI NON LEGATE AI PREVENTIVI
    # -----------------------------------------------------
    keywords = ["preventivo", "prenota", "appuntamento", "servizio", "costo", "prezzo"]
    if session["step"] == 0 and not any(k in msg for k in keywords):
        response = MessagingResponse()
        gpt_response = openai.Completion.create(
            model="text-davinci-003",
            prompt=f"Rispondi come un giardiniere esperto. Domanda: {msg}",
            max_tokens=200
        )
        response.message(gpt_response.choices[0].text.strip())
        return str(response)

    # -----------------------------------------------------
    # 2️⃣ FLUSSO PRINCIPALE
    # -----------------------------------------------------
    response = MessagingResponse()

    if session["step"] == 0:
        if "preventivo" in msg:
            session["step"] = 1
            SESSIONS[from_number] = session
            response.message(
                "Perfetto! Quale servizio ti interessa?\n"
                "• Taglio prato\n• Potatura siepi\n• Potatura alberi\n• Potatura su corda"
            )
            return str(response)

        if "prenota" in msg or "appuntamento" in msg:
            session["step"] = 10
            SESSIONS[from_number] = session
            response.message("Perfetto! Per quale servizio vuoi prenotare?")
            return str(response)

        response.message("Ciao! Vuoi un preventivo o vuoi prenotare un appuntamento?")
        return str(response)

    # -----------------------------------------------------
    # 3️⃣ PREVENTIVO – SCELTA SERVIZIO
    # -----------------------------------------------------
    if session["step"] == 1:
        if msg not in SERVICES:
            response.message("Per favore scegli uno dei servizi:\n" + "\n".join(SERVICES.keys()))
            return str(response)

        session["service"] = msg
        session["step"] = 2
        SESSIONS[from_number] = session
        response.message("Perfetto! Quante ore di lavoro prevedi?")
        return str(response)

    # -----------------------------------------------------
    # 4️⃣ PREVENTIVO – INSERIMENTO ORE
    # -----------------------------------------------------
    if session["step"] == 2:
        try:
            ore = float(msg)
        except:
            response.message("Per favore inserisci un numero valido di ore.")
            return str(response)

        session["hours"] = ore
        session["step"] = 3
        SESSIONS[from_number] = session

        prezzo_h = SERVICES[session["service"]]
        totale = prezzo_h * ore

        response.message(
            f"Il preventivo per *{session['service']}* è:\n\n"
            f"👉 {ore}h × {prezzo_h} €/h = *{totale} €*\n\n"
            "Vuoi anche prenotare un appuntamento?"
        )
        return str(response)

    # -----------------------------------------------------
    # 5️⃣ PRENOTAZIONE APPUNTAMENTO
    # -----------------------------------------------------
    if session["step"] == 10:
        session["service"] = msg
        session["step"] = 11
        SESSIONS[from_number] = session
        response.message("Perfetto! Per che giorno vuoi prenotare? (Formato: YYYY-MM-DD)")
        return str(response)

    if session["step"] == 11:
        try:
            date = datetime.strptime(msg, "%Y-%m-%d")
        except:
            response.message("Data non valida. Scrivi nel formato YYYY-MM-DD.")
            return str(response)

        start = date.replace(hour=9, minute=0)
        end = start + timedelta(hours=2)

        event = {
            "summary": f"Lavoro richiesto: {session['service']}",
            "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Rome"},
            "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Rome"},
        }

        calendar_api.events().insert(calendarId=CALENDAR_ID, body=event).execute()

        session["step"] = 0
        SESSIONS[from_number] = session

        response.message("Appuntamento confermato! A presto 🌿")
        return str(response)

    # -----------------------------------------------------
    # Fallback
    # -----------------------------------------------------
    response.message("Non ho capito. Vuoi un preventivo o prenotare?")
    return str(response)


if __name__ == "__main__":
    app.run()
