from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os

app = Flask(__name__)

# =========================
# OPENAI
# =========================
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# =========================
# MEMORIA BREVE
# =========================
SESSIONS = {}

# =========================
# PREZZI
# =========================
PRICES = {
    "siepe": 30,
    "ulivo": 35
}

# =========================
# DOMANDE GUIDATE
# =========================
QUESTIONS = {
    "siepe": [
        ("altezza_siepe", "Qual è l’altezza media della siepe (in metri)?"),
        ("accesso_siepe", "La siepe è facilmente accessibile o ci sono ostacoli?"),
        ("smaltimento", "Vuoi anche lo smaltimento del verde?")
    ],
    "ulivo": [
        ("altezza_ulivo", "Quanto sono alti gli ulivi circa?"),
        ("tipo_ulivo", "È una potatura di mantenimento o più drastica?")
    ]
}

# =========================
# HELPERS
# =========================
def get_session(num):
    if num not in SESSIONS:
        SESSIONS[num] = {
            "fase": "inizio",
            "lavori": [],
            "indice": 0,
            "dettagli": {}
        }
    return SESSIONS[num]

# =========================
# WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get("From")
    msg = request.form.get("Body", "").lower().strip()

    session = get_session(from_number)
    response = MessagingResponse()

    # ---- INIZIO ----
    if session["fase"] == "inizio":
        if "siepe" in msg:
            session["lavori"].append("siepe")
        if "ulivo" in msg:
            session["lavori"].append("ulivo")

        if session["lavori"]:
            session["fase"] = "raccolta"
            session["indice"] = 0
            lavoro = session["lavori"][0]
            key, domanda = QUESTIONS[lavoro][0]
            response.message(domanda)
            return str(response)

        response.message("Ciao! Dimmi pure che lavori di giardinaggio devi fare 😊")
        return str(response)

    # ---- RACCOLTA DATI ----
    if session["fase"] == "raccolta":
        lavoro = session["lavori"][0]
        key, _ = QUESTIONS[lavoro][session["indice"]]
        session["dettagli"][key] = msg
        session["indice"] += 1

        if session["indice"] < len(QUESTIONS[lavoro]):
            _, domanda = QUESTIONS[lavoro][session["indice"]]
            response.message(domanda)
            return str(response)

        # Passa al prossimo lavoro
        session["lavori"].pop(0)
        session["indice"] = 0

        if session["lavori"]:
            lavoro = session["lavori"][0]
            _, domanda = QUESTIONS[lavoro][0]
            response.message(domanda)
            return str(response)

        # ---- PREVENTIVO ----
        ore = 7  # stima prudente
        prezzo_min = ore * 30
        prezzo_max = prezzo_min + 60

        session["fase"] = "preventivo"
        response.message(
            f"Perfetto 👍\n"
            f"In base alle informazioni raccolte, il lavoro richiederà circa {ore}–8 ore.\n"
            f"Il preventivo indicativo è tra **{prezzo_min}€ e {prezzo_max}€**, "
            f"da confermare dopo aver visto il lavoro sul posto.\n\n"
            f"Vuoi fissare un appuntamento?"
        )
        return str(response)

    # ---- APPUNTAMENTO ----
    if session["fase"] == "preventivo" and "si" in msg:
        session["fase"] = "appuntamento"
        response.message(
            "Perfetto! Posso passare domani mattina oppure domani pomeriggio.\n"
            "Quale preferisci?"
        )
        return str(response)

    response.message("Dimmi pure 😊")
    return str(response)

# =========================
# HEALTH
# =========================
@app.route("/")
def home():
    return "Chatbot giardiniere attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
