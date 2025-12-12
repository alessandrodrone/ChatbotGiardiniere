from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import re

app = Flask(__name__)

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
    "ulivi": 35
}

# =========================
# DOMANDE OBBLIGATORIE
# =========================
QUESTIONS = {
    "siepe": [
        ("metri", "Quanti metri è lunga la siepe?"),
        ("altezza", "Qual è l’altezza media della siepe (in metri)?"),
        ("accesso", "È facilmente accessibile o ci sono zone difficili?"),
        ("smaltimento", "Vuoi anche lo smaltimento del verde?")
    ],
    "ulivi": [
        ("numero", "Quanti ulivi sono?"),
        ("altezza", "Quanto sono alti mediamente?"),
        ("tipo", "È una potatura di mantenimento o più importante?")
    ]
}

# =========================
# GPT SOLO PER INFO
# =========================
def risposta_informativa(domanda):
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Sei un giardiniere esperto."},
            {"role": "user", "content": domanda}
        ],
        max_tokens=300,
        temperature=0.4
    )
    return completion.choices[0].message.content

# =========================
# SESSION HANDLER
# =========================
def get_session(num):
    if num not in SESSIONS:
        SESSIONS[num] = {
            "fase": "inizio",
            "lavori": [],
            "lavoro_corrente": None,
            "indice_domanda": 0,
            "dati": {}
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

    # =========================
    # RISPOSTE INFORMATIVE (sempre possibili)
    # =========================
    if session["fase"] != "inizio" and "quando" in msg or "come" in msg:
        info = risposta_informativa(msg)
        response.message(info + "\n\nTorniamo un attimo al preventivo 😊")
        return str(response)

    # =========================
    # INIZIO
    # =========================
    if session["fase"] == "inizio":
        if "siepe" in msg:
            session["lavori"].append("siepe")
        if "ulivo" in msg:
            session["lavori"].append("ulivi")

        if session["lavori"]:
            session["fase"] = "dettagli"
            session["lavoro_corrente"] = session["lavori"][0]
            session["dati"][session["lavoro_corrente"]] = {}
            session["indice_domanda"] = 0

            _, domanda = QUESTIONS[session["lavoro_corrente"]][0]
            response.message(domanda)
            return str(response)

        response.message("Dimmi pure che lavori di giardinaggio devi fare 😊")
        return str(response)

    # =========================
    # RACCOLTA DETTAGLI
    # =========================
    if session["fase"] == "dettagli":
        lavoro = session["lavoro_corrente"]
        key, _ = QUESTIONS[lavoro][session["indice_domanda"]]
        session["dati"][lavoro][key] = msg
        session["indice_domanda"] += 1

        if session["indice_domanda"] < len(QUESTIONS[lavoro]):
            _, domanda = QUESTIONS[lavoro][session["indice_domanda"]]
            response.message(domanda)
            return str(response)

        # lavoro completato
        session["lavori"].pop(0)
        session["indice_domanda"] = 0

        if session["lavori"]:
            session["lavoro_corrente"] = session["lavori"][0]
            session["dati"][session["lavoro_corrente"]] = {}
            _, domanda = QUESTIONS[session["lavoro_corrente"]][0]
            response.message(domanda)
            return str(response)

        # =========================
        # PREVENTIVO
        # =========================
        ore = 8
        prezzo_min = ore * 30
        prezzo_max = prezzo_min + 80

        session["fase"] = "preventivo"

        response.message(
            f"Perfetto 👍\n"
            f"In base ai dati raccolti il lavoro richiede circa {ore} ore.\n"
            f"Il preventivo indicativo è tra **{prezzo_min}€ e {prezzo_max}€**, "
            f"da confermare dopo sopralluogo.\n\n"
            f"Vuoi fissare un appuntamento?"
        )
        return str(response)

    # =========================
    # APPUNTAMENTO
    # =========================
    if session["fase"] == "preventivo" and "si" in msg:
        session["fase"] = "appuntamento"
        response.message(
            "Perfetto! Serve una mezza giornata intera.\n"
            "Posso venire domani mattina oppure domani pomeriggio.\n"
            "Cosa preferisci?"
        )
        return str(response)

    response.message("Dimmi pure 😊")
    return str(response)

# =========================
@app.route("/")
def home():
    return "Chatbot giardiniere attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
