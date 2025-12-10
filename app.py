from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os

app = Flask(__name__)

# OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
    """Usa GPT-4o mini per capire cosa vuole il cliente."""
    prompt = f"""
Sei un assistente per un giardiniere professionista.

1. Analizza il messaggio dell’utente qui sotto.
2. Capisci TUTTE le intenzioni presenti (es: domanda botanica, preventivo, appuntamento).
3. Estrai eventuali servizi (dal dizionario più sotto) anche se scritti in modo non preciso.
4. Se l’utente chiede informazioni su piante, rispondi con competenza reale.
5. Se chiede preventivo: calcola un preventivo realistico basato sui servizi.
6. Se chiede appuntamento: chiedi solo data/ora.
7. Rispondi in modo naturale, come ChatGPT, conversazionale e intelligente.

📌 Dizionario servizi disponibili:
{SERVICES}

📌 Messaggio utente:
--------------------
{user_message}
--------------------

Genera una risposta finale naturale, chiara, amichevole MA PROFESSIONALE.
"""

    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    return completion.choices[0].message["content"]

@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    user_message = request.form.get("Body")
    reply_text = analyze_message(user_message)

    response = MessagingResponse()
    response.message(reply_text)
    return str(response)


@app.route("/")
def home():
    return "Bot attivo."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
