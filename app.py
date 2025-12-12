from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os

app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# =========================
# MEMORIA BREVE
# =========================
SESSIONS = {}

# =========================
# PREZZI ORARI
# =========================
PRICES = {
    "siepe": 30,
    "ulivi": 35
}

# =========================
# CHECKLIST OBBLIGATORIE
# =========================
CHECKLIST = {
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
# SESSIONE
# =========================
def get_session(num):
    if num not in SESSIONS:
        SESSIONS[num] = {
            "fase": "raccolta_lavori",
            "lavori": [],
            "lavoro_corrente": None,
            "campo_corrente": 0,
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
    # FASE 1 – RACCOLTA LAVORI
    # =========================
    if session["fase"] == "raccolta_lavori":
        if "siepe" in msg:
            session["lavori"].append("siepe")
        if "ulivo" in msg:
            session["lavori"].append("ulivi")

        if not session["lavori"]:
            response.message("Dimmi pure che lavori di giardinaggio devi fare 😊")
            return str(response)

        session["lavoro_corrente"] = session["lavori"][0]
        session["dati"][session["lavoro_corrente"]] = {}
        session["campo_corrente"] = 0
        session["fase"] = "raccolta_dettagli"

        _, domanda = CHECKLIST[session["lavoro_corrente"]][0]
        response.message(domanda)
        return str(response)

    # =========================
    # FASE 2 – RACCOLTA DETTAGLI
    # =========================
    if session["fase"] == "raccolta_dettagli":
        lavoro = session["lavoro_corrente"]
        campo, _ = CHECKLIST[lavoro][session["campo_corrente"]]
        session["dati"][lavoro][campo] = msg
        session["campo_corrente"] += 1

        # Se ci sono ancora campi
        if session["campo_corrente"] < len(CHECKLIST[lavoro]):
            _, domanda = CHECKLIST[lavoro][session["campo_corrente"]]
            response.message(domanda)
            return str(response)

        # Passa al prossimo lavoro
        session["lavori"].pop(0)

        if session["lavori"]:
            session["lavoro_corrente"] = session["lavori"][0]
            session["dati"][session["lavoro_corrente"]] = {}
            session["campo_corrente"] = 0
            _, domanda = CHECKLIST[session["lavoro_corrente"]][0]
            response.message(domanda)
            return str(response)

        # =========================
        # FASE 3 – PREVENTIVO
        # =========================
        session["fase"] = "preventivo"

        ore_siepe = 6
        ore_ulivi = 2 if "ulivi" in session["dati"] else 0
        ore_totali = ore_siepe + ore_ulivi

        prezzo_min = ore_totali * 30
        prezzo_max = prezzo_min + 80

        response.message(
            f"Perfetto 👍\n"
            f"In base ai dati raccolti il lavoro richiederà circa {ore_totali}–{ore_totali + 1} ore.\n"
            f"Il preventivo indicativo è tra **{prezzo_min}€ e {prezzo_max}€**, "
            f"da confermare dopo sopralluogo.\n\n"
            f"Vuoi fissare un appuntamento?"
        )
        return str(response)

    # =========================
    # FASE 4 – APPUNTAMENTO
    # =========================
    if session["fase"] == "preventivo" and "si" in msg:
        session["fase"] = "appuntamento"
        response.message(
            "Perfetto! Serve una mezza giornata intera.\n"
            "Posso venire domani mattina (8:00–14:00) oppure domani pomeriggio (13:00–19:00).\n"
            "Cosa preferisci?"
        )
        return str(response)

    # =========================
    response.message("Se hai altre domande sul giardino, sono qui 😊")
    return str(response)

# =========================
@app.route("/")
def home():
    return "Chatbot giardiniere attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
