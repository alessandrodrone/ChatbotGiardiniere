from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import re

app = Flask(__name__)

# =========================
# MEMORIA BREVE (SESSIONI)
# =========================
SESSIONS = {}

# =========================
# OPENAI CLIENT
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("La variabile di ambiente OPENAI_API_KEY non è impostata.")

client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# SERVIZI E PREZZI
# =========================
SERVICES = {
    "taglio prato": 25,
    "potatura siepi": 30,
    "potatura alberi": 35,
    "potatura su corda": 60,
    "trattamenti antiparassitari": 40,
    "pulizia giardino": 28,
    "raccolta foglie": 25,
    "smaltimento verde": 30,
}

CONFERME = ["ok", "va bene", "confermiamo", "sì", "si", "perfetto"]

# =========================
# SYSTEM PROMPT
# =========================
SYSTEM_PROMPT = f"""
Sei un assistente WhatsApp professionale dedicato ESCLUSIVAMENTE a un giardiniere.

Parla solo di giardinaggio, piante, manutenzione del verde, preventivi e appuntamenti.
Non rispondere mai a domande fuori ambito.

Prezzi orari (€/h):
{SERVICES}

Quando proponi un appuntamento, usa frasi chiare come:
"Posso venire domani alle 14:00" oppure "Settimana prossima".

Se il cliente conferma, considera l’appuntamento accettato.
"""

# =========================
# PROMPT UTENTE
# =========================
def build_user_prompt(user_message: str) -> str:
    return f"""
Messaggio cliente:
\"\"\"{user_message}\"\"\"

Rispondi come assistente del giardiniere, in modo chiaro e professionale.
"""

# =========================
# OPENAI CALL
# =========================
def analyze_message(user_message: str) -> str:
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(user_message)},
        ],
        max_tokens=500,
        temperature=0.4,
    )
    return completion.choices[0].message.content

# =========================
# WEBHOOK WHATSAPP
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    from_number = request.form.get("From")
    user_message = request.form.get("Body", "").strip()

    if not user_message:
        user_message = "Ciao"

    session = SESSIONS.get(from_number, {})

    # 1️⃣ GESTIONE CONFERMA APPUNTAMENTO
    if session.get("stato") == "in_attesa_conferma":
        if user_message.lower() in CONFERME:
            reply_text = (
                f"Perfetto! Ho confermato l’intervento per "
                f"{session.get('servizio')} "
                f"({session.get('dettagli')}) "
                f"il {session.get('appuntamento')}.\n"
                "Se hai altre domande sul tuo giardino, sono qui!"
            )

            session["stato"] = "confermato"
            SESSIONS[from_number] = session

            response = MessagingResponse()
            response.message(reply_text)
            return str(response)

    # 2️⃣ RISPOSTA CON OPENAI
    reply_text = analyze_message(user_message)

    # 3️⃣ SALVATAGGIO STATO (semplice ma efficace)
    lower_reply = reply_text.lower()

    if "potatura siepi" in lower_reply:
        session["servizio"] = "potatura siepi"
    elif "taglio prato" in lower_reply:
        session["servizio"] = "taglio prato"

    metri = re.search(r"(\d+)\s*metri", user_message.lower())
    if metri:
        session["dettagli"] = f"{metri.group(1)} metri"

    if "domani" in lower_reply or "settimana" in lower_reply:
        session["appuntamento"] = reply_text
        session["stato"] = "in_attesa_conferma"

    SESSIONS[from_number] = session

    response = MessagingResponse()
    response.message(reply_text)
    return str(response)

# =========================
# HEALTH CHECK
# =========================
@app.route("/")
def home():
    return "Chatbot giardiniere attivo ✅"

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
