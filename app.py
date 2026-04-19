from flask import Flask, render_template, request, redirect, session, flash, url_for
import os
import mysql.connector
from datetime import datetime
from functools import wraps
from groq import Groq
import requests

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "default_secret_key")

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
HF_TOKEN = os.getenv("HF_TOKEN")

def get_db():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password=os.getenv("DB_PASSWORD"),
        database="health_camp"
    )

TRIAGE_SYSTEM = """
You are a clinical assistant in a health camp.

Rules:
- Always give medical suggestions with real medicines.
- Be short and practical.

PRIORITY RULES (STRICT):


- HIGH: chest pain, difficulty breathing, unconsciousness, severe bleeding, stroke symptoms

- MEDIUM: fever, infection, vomiting, weakness, persistent headache, moderate pain,
  eye redness, conjunctival injection, skin rash, itching, hives, allergic reaction, dermatitis, insect bite reaction

- LOW: mild cough, common cold, sneezing, mild throat irritation, minor pain
AGE RULE:
- If age >= 60, increase severity by one level (LOW→MEDIUM, MEDIUM→HIGH)

IMPORTANT:
- Always follow priority rules strictly.
- Do NOT guess severity.
- Be conservative for mild symptoms.

Output EXACTLY 4 lines:

Line 1: HIGH or MEDIUM or LOW

Line 2: Prescribe medicine with dose
Example:
fever → Prescribe Paracetamol 500mg every 8 hours
eye redness → Prescribe Moxifloxacin eye drops
cough → Prescribe Ambroxol syrup
allergy → Prescribe Cetirizine
pain → Prescribe Ibuprofen

Line 3: Second medicine or test

Line 4: Short monitoring advice
"""

def analyze_reason(reason, age, image_file=None):
    priority = "MEDIUM"
    condition_suggestions = ""
    image_analysis = "No image provided."

    import base64  # (safe to include inside function if needed)

    if image_file and image_file.filename:
        try:
            image_file.seek(0)
            image_bytes = image_file.read()

            if not image_bytes:
                image_analysis = "Empty image file received."
            else:
                ext = image_file.filename.rsplit(".", 1)[1].lower()

                mime_map = {
                    "jpg": "image/jpeg",
                    "jpeg": "image/jpeg",
                    "png": "image/png",
                    "webp": "image/webp",
                    "gif": "image/gif",
                    "bmp": "image/bmp"
                }

                mime_type = mime_map.get(ext, "image/jpeg")

                b64_image = base64.b64encode(image_bytes).decode("utf-8")

                response = groq_client.chat.completions.create(
                    model="meta-llama/llama-4-scout-17b-16e-instruct",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{mime_type};base64,{b64_image}"
                                    }
                                },
                                {
                                    "type": "text",
                                    "text": (
                                        "Describe only what is visible in the image in simple words. "
                                        "Do NOT diagnose. Just describe visual appearance."
                                    )
                                }
                            ]
                        }
                    ],
                    max_tokens=200,
                    temperature=0.2
                )

                image_analysis = response.choices[0].message.content.strip()

        except Exception as e:
            image_analysis = f"Image analysis error: {str(e)}"
    try:
        chat = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": TRIAGE_SYSTEM},
                {"role": "user", "content": f"Symptoms: {reason}, Age: {age}"}
            ],
            temperature=0.2,
        )

        output = chat.choices[0].message.content.strip()
        lines = [l.strip() for l in output.splitlines() if l.strip()]

        if lines and lines[0].upper() in ["HIGH", "MEDIUM", "LOW"]:
            priority = lines[0].upper()
            condition_suggestions = "\n".join(lines[1:4])
        else:
            condition_suggestions = output

    except Exception as e:
        condition_suggestions = f"Triage error: {e}"

    # AGE BOOST
    if int(age) >= 60:
        if priority == "LOW":
            priority = "MEDIUM"
        elif priority == "MEDIUM":
            priority = "HIGH"

    return priority, condition_suggestions, image_analysis


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form["username"] == "admin" and request.form["password"] == "admin":
            session['user'] = "admin"
            return redirect("/")
        flash("Invalid credentials")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@login_required
def home():
    db = get_db()
    cur = db.cursor(dictionary=True)

    cur.execute("""
        SELECT p.*, d.name as doctor_name 
        FROM patients p 
        LEFT JOIN doctors d ON p.doctor_id = d.doctor_id 
        WHERE p.status = 'waiting'
        ORDER BY 
            CASE p.is_priority WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, 
            p.arrival_time ASC
    """)

    patients = cur.fetchall()

    cur.execute("""
        SELECT d.name, d.specialization, COUNT(p.token) as patient_count
        FROM doctors d 
        LEFT JOIN patients p ON d.doctor_id = p.doctor_id AND p.status = 'waiting'
        GROUP BY d.doctor_id, d.name, d.specialization
    """)

    doctors_load = cur.fetchall()
    db.close()

    return render_template("index.html", patients=patients, doctors_load=doctors_load)


@app.route("/add", methods=["POST"])
@login_required
def add():
    name = request.form["name"]
    age = int(request.form["age"])
    reason = request.form["reason"]
    contact = request.form.get("contact", "")
    gender = request.form.get("gender", "Male")
    address = request.form.get("address", "")
    image_file = request.files.get("image")

    priority, suggestions, img_analysis = analyze_reason(reason, age, image_file)

    db = get_db()
    cur = db.cursor()

    cur.execute("""
        SELECT d.doctor_id FROM doctors d 
        LEFT JOIN patients p ON d.doctor_id = p.doctor_id AND p.status = 'waiting' 
        GROUP BY d.doctor_id ORDER BY COUNT(p.token) ASC LIMIT 1
    """)

    doc_res = cur.fetchone()
    doctor_id = doc_res[0] if doc_res else 1

    cur.execute("""
        SELECT COUNT(*) FROM patients 
        WHERE doctor_id = %s AND status = 'waiting'
    """, (doctor_id,))

    queue_size = cur.fetchone()[0]

    priority_buffer = {"HIGH": 5, "MEDIUM": 15, "LOW": 25}
    est_wait = (queue_size * 15) + priority_buffer.get(priority, 25)

    cur.execute("SELECT IFNULL(MAX(camp_token), 0) + 1 FROM patients")
    next_token = cur.fetchone()[0]

    cur.execute("""
        INSERT INTO patients 
        (name, reason, doctor_id, is_priority, age, contact, gender, address,
         status, estimated_wait, camp_token, condition_suggestions, image_analysis) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'waiting', %s, %s, %s, %s)
    """, (name, reason, doctor_id, priority, age, contact, gender, address,
          est_wait, next_token, suggestions, img_analysis))

    db.commit()
    db.close()

    return redirect("/")


@app.route("/serve")
@login_required
def serve():
    db = get_db()
    cur = db.cursor()

    cur.execute("""
        SELECT token FROM patients 
        WHERE status = 'waiting' 
        ORDER BY CASE is_priority WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
                 arrival_time ASC 
        LIMIT 1
    """)

    res = cur.fetchone()

    if res:
        cur.execute("UPDATE patients SET status = 'served' WHERE token = %s", (res[0],))
        db.commit()

    db.close()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=True)