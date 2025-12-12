from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import re
from datetime import datetime, timedelta

app = Flask(__name__)

# =========================
# CONFIG
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

WORK_START_HOUR = int(os.getenv("WORK_START_HOUR", 8))
WORK_END_HOUR = int(os.getenv("WORK_END_HOUR", 18))

client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# SERVIZI
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
# DATABASE
# =========================
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                phone TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS sessions (
                phone TEXT PRIMARY KEY,
                state TEXT,
                service TEXT,
                meters INT,
                height FLOAT,
                trees INT,
                estimated_hours INT,
                estimated_price INT,
                updated_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id SERIAL PRIMARY KEY,
                phone TEXT,
                description TEXT,
                hours INT,
                price INT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """)
        conn.commit()

init_db()

# =========================
# AI SOLO PER TESTO
# =========================
SYSTEM_PROMPT = """
Sei un assistente WhatsApp professionale per un giardiniere.
Parla SOLO di giardinaggio, piante, preventivi e appuntamenti.
Non inventare prezzi o orari.
"""

def ai_reply(text):
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0.3,
        max_tokens=300,
    )
    return completion.choices[0].message.content

# =========================
# LOGICA PREVENTIVI
# =========================
def estimate_siepe(meters, height):
    hours = meters / 50
    if height >= 2:
        hours *= 1.5
    return round(hours)

def next_available_slot(hours):
    start = datetime.now().replace(hour=WORK_START_HOUR, minute=0, second=0)
    if datetime.now().hour >= WORK_END_HOUR:
        start += timedelta(days=1)
    end = start + timedelta(hours=hours)
    return start, end

# =========================
# WEBHOOK WHATSAPP
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_bot():
    phone = request.form.get("From")
    msg = request.form.get("Body", "").lower().strip()
    response = MessagingResponse()

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clients (phone) VALUES (%s) ON CONFLICT DO NOTHING;", (phone,))
            cur.execute("SELECT * FROM sessions WHERE phone=%s;", (phone,))
            session = cur.fetchone()

    # === NUOVA SESSIONE ===
    if not session:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (phone, state) VALUES (%s,'start');",
                    (phone,),
                )
            conn.commit()
        response.message("Ciao! Dimmi pure che lavoro di giardinaggio devi fare 🙂")
        return str(response)

    state = session["state"]

    # === STEP 1: SERVIZIO ===
    if state == "start":
        if "siepe" in msg:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE sessions SET state='siepe_meters' WHERE phone=%s;",
                        (phone,),
                    )
                conn.commit()
            response.message("Quanti metri è lunga la siepe?")
            return str(response)

        return response.message(ai_reply(msg))

    # === STEP 2: METRI SIEPE ===
    if state == "siepe_meters":
        meters = re.search(r"\d+", msg)
        if meters:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE sessions SET meters=%s, state='siepe_height' WHERE phone=%s;",
                        (int(meters.group()), phone),
                    )
                conn.commit()
            response.message("Che altezza ha indicativamente la siepe?")
            return str(response)

        response.message("Indicami i metri della siepe (es. 300).")
        return str(response)

    # === STEP 3: ALTEZZA ===
    if state == "siepe_height":
        try:
            height = float(msg.replace(",", "."))
            hours = estimate_siepe(session["meters"], height)
            price = hours * SERVICES["potatura siepi"]

            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE sessions
                        SET height=%s, estimated_hours=%s, estimated_price=%s, state='confirm'
                        WHERE phone=%s;
                        """,
                        (height, hours, price, phone),
                    )
                conn.commit()

            response.message(
                f"Perfetto 👍\n"
                f"• Tempo stimato: circa {hours} ore\n"
                f"• Prezzo indicativo: {price} €\n\n"
                f"Il prezzo è da confermare dopo sopralluogo.\n"
                f"Vuoi che fissiamo l’intervento?"
            )
            return str(response)
        except:
            response.message("Indicami l’altezza in metri (es. 2).")
            return str(response)

    # === STEP 4: CONFERMA ===
    if state == "confirm" and msg in ["si", "sì", "ok", "va bene", "confermiamo"]:
        start, end = next_available_slot(session["estimated_hours"])

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jobs (phone, description, hours, price)
                    VALUES (%s,%s,%s,%s);
                    DELETE FROM sessions WHERE phone=%s;
                    """,
                    (
                        phone,
                        "Potatura siepe",
                        session["estimated_hours"],
                        session["estimated_price"],
                        phone,
                    ),
                )
            conn.commit()

        response.message(
            f"Perfetto! ✅\n"
            f"Ti segno per {start.strftime('%d/%m')} dalle {start.strftime('%H:%M')} "
            f"alle {end.strftime('%H:%M')}.\n"
            f"A presto!"
        )
        return str(response)

    return response.message(ai_reply(msg))

@app.route("/")
def home():
    return "Chatbot giardiniere attivo ✅"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
