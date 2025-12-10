from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
from collections import defaultdict

app = Flask(__name__)

# ✅ OpenAI client
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("Devi impostare la variabile OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Servizi disponibili e prezzi €/h
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

# Memoria sessione utenti (telefono -> storia conversazione)
user_sessions = defaultdict(list)

def analyze_message(user_message, session_history):
    """
    Usa GPT-4o-mini per analizzare il messaggio e rispondere in modo intelligente.
    """
    # Aggiunge il messaggio alla storia
    session_history.append({"role": "user", "content": user_message})

    # Prompt base con istruzioni
    prompt = f"""
Sei un assistente virtuale professionale per giardinieri. Analizza le richieste dell'utente e rispondi in modo naturale e chiaro.

1. Identifica tutte le intenzioni dell'utente: domanda botanica, preventivo, appuntamento.
2. Estrai eventuali servizi citati nel dizionario sottostante anche se scritti in modo approssimativo.
3. Se l'utente chiede preventivo, calcola costo realistico in base ai servizi menzionati.
4. Se l'utente vuole prenotare un appuntamento, chiedi solo data e ora.
5. Mantieni un tono amichevole ma professionale.
6. Rispondi come farebbe ChatGPT.

📌 Servizi e prezzi €/h:
{SERVICES}

📌 Conversazione finora:
{session_history}

Rispondi con una risposta completa, chiara e naturale.
"""
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=session_history + [{"role": "user", "content": prompt}],
            max_tokens=600
        )
        reply = completion.choices[0].message["content"].strip()
        # Aggiunge la risposta alla storia
        session_history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        return f"Errore nel generare la risposta: {str(e)}"

@app.route("/webhook", methods=["POST"])
def whatsapp_bot():
    user_message = request.form.get("Body")
    from_number = request.form.get("From")  # numero utente WhatsApp

    if not user_message or not from_number:
        return "Messaggio o numero utente non disponibile.", 400

    session_history = user_sessions[from_number]  # recupera conversazione precedente
    reply_text = analyze_message(user_message, session_history)

    response = MessagingResponse()
    response.message(reply_text)
    return str(response)

@app.route("/")
def home():
    return "Bot avanzato attivo e pronto a ricevere messaggi WhatsApp!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
