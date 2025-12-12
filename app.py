from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os

app = Flask(__name__)

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

# =========================
# SYSTEM PROMPT ANTI-DERIVA
# =========================
SYSTEM_PROMPT = f"""
Sei un assistente WhatsApp professionale dedicato ESCLUSIVAMENTE a un giardiniere.

Il tuo compito è:
- rispondere ai clienti del giardiniere
- fornire informazioni su servizi di giardinaggio
- dare consigli pratici su piante e manutenzione del verde
- preparare preventivi indicativi
- aiutare a fissare appuntamenti

NON sei un assistente generico.
NON devi mai uscire dall’ambito del giardinaggio.

PUOI parlare SOLO di:
- giardinaggio
- piante, alberi, siepi, prati
- potature e stagionalità
- manutenzione del verde
- preventivi
- appuntamenti
- servizi offerti dal giardiniere

NON devi rispondere a domande su:
- politica
- medicina
- psicologia
- finanza
- tecnologia
- legge
- argomenti non legati al giardinaggio

Se ricevi una domanda fuori ambito:
- NON rispondere nel merito
- Riporta educatamente la conversazione sui servizi di giardinaggio

Stile di risposta:
- professionale
- chiaro
- concreto
- amichevole ma serio
- linguaggio semplice

Prezzi orari disponibili (€/h):
{SERVICES}

Quando fai un preventivo:
- spiega cosa è incluso
- specifica che è indicativo
- proponi un appuntamento se opportuno

Obiettivo finale:
aiutare il cliente e trasformare la conversazione in un lavoro reale.
"""

# =========================
# USER PROMPT
# =========================
def build_user_prompt(user_message: str) -> str:
    return f"""
Messaggio del cliente:
\"\"\"{user_message}\"\"\"

Rispondi seguendo rigorosamente il tuo ruolo di assistente per giardiniere.
"""

# =========================
# OPENAI CALL
# =========================
def analyze_message(user_message: str) -> str:
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(user_message)},
            ],
            max_tokens=500,
            temperature=0.4,  # stabilità > fantasia
        )

        return completion.choices[0].message.content

    except Exception as e:
        return (
            "Al momento ho un problema tecnico e non riesco a rispondere correttamente. "
            "Riprova tra poco o scrivimi cosa ti serve per il tuo giardino."
        )

# =========================
# TWILIO WEBHOOK
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    user_message = request.form.get("Body", "").strip()
    if not user_message:
        user_message = "Ciao"

    reply_text = analyze_message(user_message)

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
