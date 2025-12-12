from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import re

app = Flask(__name__)

# =========================
# OPENAI
# =========================
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# =========================
# MEMORIA BREVE (SESSIONI)
# =========================
SESSIONS = {}

def init_session():
    return {
        "fase": "raccolta",
        "lavori": {
            "siepe": {
                "metri": None,
                "altezza": None,
                "accesso": None,
                "smaltimento": None
            },
            "ulivi": {
                "numero": None,
                "altezza": None
            },
            "prato": {
                "mq": None,
                "pendenza": None
            },
            "corda": {
                "tipo": None,
                "altezza": None
            }
        },
        "preventivo_pronto": False
    }

# =========================
# PREZZI
# =========================
PREZZI = {
    "siepe": 30,
    "ulivi": 35,
    "prato": 25,
    "corda": 60,
    "antiparassitari": 40,
    "pulizia": 28,
    "foglie": 25,
    "smaltimento": 30
}

# =========================
# DOMANDE PER DATI
# =========================
DOMANDE = {
    ("siepe", "metri"): "Quanti metri è lunga la siepe?",
    ("siepe", "altezza"): "Qual è l’altezza media della siepe?",
    ("siepe", "accesso"): "Ci sono zone difficili da raggiungere?",
    ("siepe", "smaltimento"): "Vuoi anche lo smaltimento del verde?",

    ("ulivi", "numero"): "Quanti ulivi ci sono?",
    ("ulivi", "altezza"): "Quanto sono alti mediamente gli ulivi?",

    ("prato", "mq"): "Quanti metri quadri è il prato?",
    ("prato", "pendenza"): "Il prato è pianeggiante o in pendenza?",

    ("corda", "tipo"): "Che tipo di albero va potato?",
    ("corda", "altezza"): "Quanto è alto l’albero?"
}

# =========================
# UTIL
# =========================
def estrai_numero(testo):
    m = re.search(r"\d+", testo)
    return int(m.group()) if m else None

def lavoro_richiesto(msg, lavoro):
    return lavoro in msg.lower()

# =========================
# LOGICA CORE
# =========================
def prossima_domanda(session):
    for lavoro, campi in session["lavori"].items():
        if any(v is not None for v in campi.values()):
            for campo, valore in campi.items():
                if valore is None:
                    return DOMANDE.get((lavoro, campo))
    return None

def calcola_preventivo(session):
    totale_ore = 0
    dettagli = []

    siepe = session["lavori"]["siepe"]
    if siepe["metri"]:
        ore = siepe["metri"] / 40
        if siepe["altezza"] >= 2:
            ore += 1
        totale_ore += ore
        dettagli.append(f"Sievpe: ~{round(ore,1)}h")

    ulivi = session["lavori"]["ulivi"]
    if ulivi["numero"]:
        ore = ulivi["numero"] * 1.5
        totale_ore += ore
        dettagli.append(f"Ulivi: ~{round(ore,1)}h")

    costo = totale_ore * 30
    return totale_ore, round(costo)

# =========================
# AI SOLO PER INFO
# =========================
def risposta_ai(domanda):
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Rispondi solo a domande di giardinaggio."},
            {"role": "user", "content": domanda}
        ],
        temperature=0.3,
        max_tokens=200
    )
    return completion.choices[0].message.content

# =========================
# WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    numero = request.form.get("From")
    msg = request.form.get("Body", "").lower().strip()

    if numero not in SESSIONS:
        SESSIONS[numero] = init_session()

    session = SESSIONS[numero]

    # 1️⃣ RICONOSCIMENTO LAVORI
    for lavoro in session["lavori"]:
        if lavoro_richiesto(msg, lavoro):
            pass  # attivato implicitamente

    # 2️⃣ SALVATAGGIO DATI
    n = estrai_numero(msg)
    for lavoro, campi in session["lavori"].items():
        for campo in campi:
            if campi[campo] is None:
                if campo in ["metri", "altezza", "numero", "mq"] and n:
                    campi[campo] = n
                    break
                if campo == "smaltimento" and msg in ["si", "sì", "no"]:
                    campi[campo] = msg
                    break

    # 3️⃣ PROSSIMA DOMANDA
    domanda = prossima_domanda(session)
    if domanda:
        reply = domanda
    else:
        ore, costo = calcola_preventivo(session)
        reply = (
            f"In base ai dati raccolti il lavoro richiede circa {round(ore,1)} ore.\n"
            f"Preventivo indicativo: *{costo}€*.\n"
            "Il prezzo definitivo verrà confermato dopo sopralluogo.\n\n"
            "Vuoi fissare un appuntamento?"
        )

    response = MessagingResponse()
    response.message(reply)
    return str(response)

@app.route("/")
def home():
    return "Chatbot giardiniere attivo ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
