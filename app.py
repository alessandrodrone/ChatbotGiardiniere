from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os

app = Flask(__name__)

# ✅ OpenAI client
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("Devi impostare la variabile OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Dizionario servizi e prezzi
SERVICES = {
    "taglio prato": 25,
    "potatura siepi": 30,
    "potatura alberi": 35,
    "potatura su corda": 60,
    "trattamenti antiparassitari": 40,
    "pulizia giardino": 28,
    "raccolta foglie": 25,
    "smaltimento verde": 30
}

def analyze_message(user_message):
    """
    Analizza il messaggio dell'utente usando GPT-4o-mini
    e decide se rispondere a domande, preventivi o appuntamenti.
    """
    prompt = f"""
Sei un assistente virtuale per un giardiniere professionista. 
Il tuo compito è rispondere in modo naturale, chiaro, conversazionale e professionale.

1. Identifica le intenzioni dell'utente: domanda botanica, preventivo, appuntamento.
2. Se menziona un servizio dal dizionario sottostante, calcola eventuali preventivi.
3. Se chiede appuntamento, richiedi solo data/ora.
4. Rispondi anche a domande su piante o giardinaggio con competenza reale.
5. Mantieni sempre un tono amichevole ma professionale.

📌 Servizi disponibili e prezzi €/h:
{SERVICES}

📌 Messaggio dell'utente:
------------------------
{user_message}
------------------------

Rispondi con una sola risposta completa, chiara e naturale.
"""
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        return completion.choices[0].message["content"].strip()
    except Exception as e:
        return f"Errore nel generare la risposta: {str(e)}"

@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    user_message = request.form.get("Body")
    if not user_message:
        user_message = "Ciao"

    reply_text = analyze_message(user_message)

    response = MessagingResponse()
    response.message(reply_text)
    return str(response)

@app.route("/")
def home():
    return "Bot attivo e pronto a ricevere messaggi WhatsApp!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
