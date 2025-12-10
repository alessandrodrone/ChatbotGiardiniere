from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os

app = Flask(__name__)

# OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Servizi e prezzi
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
    """Usa GPT-4o mini per risposte intelligenti e comprensione conversazionale."""
    
    prompt = f"""
Sei un assistente professionale per un giardiniere.

COMPITI:
1. Rispondi in modo intelligente a QUALSIASI domanda su piante, giardino, potature, stagionalità, malattie, ecc.
2. Se nel messaggio è presente una richiesta di preventivo → genera un preventivo realistico usando questi prezzi:
{SERVICES}
3. Se c’è una richiesta di appuntamento → chiedi data e ora.
4. Rispondi SEMPRE in modo naturale, amichevole e professionale.
5. Non forzare il cliente a seguire un flusso: capisci tu cosa vuole.

Messaggio cliente:
"{user_message}"
"""

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Sei un assistente intelligente per un giardiniere professionista."},
                {"role": "user", "content": prompt}
            ]
        )

        # ❗ NUOVA SINTASSI
        return completion.choices[0].message.content

    except Exception as e:
        return f"Errore nel generare la risposta: {str(e)}"


@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    user_message = request.form.get("Body", "")
    reply_text = analyze_message(user_message)

    response = MessagingResponse()
    response.message(reply_text)
    return str(response)


@app.route("/")
def home():
    return "Bot attivo e funzionante!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
