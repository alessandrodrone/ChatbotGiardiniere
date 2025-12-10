from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os

app = Flask(__name__)

# Inizializza client OpenAI con la chiave dalle variabili d'ambiente
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("La variabile di ambiente OPENAI_API_KEY non è impostata.")
client = OpenAI(api_key=OPENAI_API_KEY)

# Servizi e prezzi orari
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

def build_prompt(user_message: str) -> str:
    """
    Costruisce un prompt chiaro per il modello.
    """
    return f"""
Sei un assistente virtuale professionale per un giardiniere.

- Rispondi a QUALSIASI domanda di giardinaggio (piante, potature, stagioni, malattie, concimi, ecc.).
- Se l'utente chiede un PREVENTIVO, usa questi servizi e prezzi orari (€/h):
  {SERVICES}
  Stima le ore in modo realistico se l'utente dà dettagli (es. metri di siepe, dimensioni giardino).
- Se l'utente chiede un APPUNTAMENTO, non fissare davvero nulla nel calendario, ma:
  - proponi alcune fasce orarie possibili (es. domani pomeriggio, settimana prossima, ecc.)
  - chiedi conferma.
- Rispondi in italiano, in modo naturale, amichevole ma professionale.
- Non fare domande inutili se la richiesta è già chiara.

Messaggio dell'utente:
\"\"\"{user_message}\"\"\"
"""

def analyze_message(user_message: str) -> str:
    """
    Chiama OpenAI (gpt-4o-mini) e restituisce la risposta testuale.
    """
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Sei un assistente intelligente specializzato in giardinaggio e gestione clienti via WhatsApp.",
                },
                {
                    "role": "user",
                    "content": build_prompt(user_message),
                },
            ],
            max_tokens=600,
        )

        # ✅ Sintassi corretta con libreria nuova
        message_obj = completion.choices[0].message
        # message_obj è un ChatCompletionMessage, usiamo l'attributo .content
        return message_obj.content

    except Exception as e:
        # In caso di errore, restituiamo un messaggio amichevole invece di crashare
        return f"Al momento ho un problema tecnico con l'AI ({type(e).__name__}: {e}). Riprova tra poco, per favore."

@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    """
    Endpoint chiamato da Twilio quando arriva un messaggio WhatsApp.
    """
    user_message = request.form.get("Body", "").strip()

    if not user_message:
        user_message = "Ciao"

    reply_text = analyze_message(user_message)

    twilio_response = MessagingResponse()
    twilio_response.message(reply_text)
    return str(twilio_response)

@app.route("/")
def home():
    return "Chatbot giardiniere attivo ✅"

if __name__ == "__main__":
    # Su Railway la porta arriva dalla variabile PORT
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
