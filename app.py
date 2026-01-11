from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta, datetime
import os
import random
import time

# ===== ASR imports =====
import numpy as np
import torch
import soundfile as sf
import librosa
from transformers import WhisperProcessor, WhisperForConditionalGeneration
from peft import PeftModel

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- CONFIGURATION ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_FOLDER = os.path.join(BASE_DIR, "instance")
if not os.path.exists(INSTANCE_FOLDER):
    os.makedirs(INSTANCE_FOLDER)

db_path = os.path.join(INSTANCE_FOLDER, "medical_scribe.db")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + db_path
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.permanent_session_lifetime = timedelta(minutes=60)

# Optional: allow larger uploads if needed
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB

db = SQLAlchemy(app)

# ===== ASR CONFIG =====
BASE_MODEL_ID = "mesolitica/malaysian-whisper-medium-v2"
ADAPTER_DIR = "rojak_medium_lora_adapter"  # folder path (place beside app.py or give absolute path)
USE_LORA = True
TARGET_SR = 16000

# Lazy-loaded ASR objects
_ASR = {"processor": None, "model": None}

# In-memory transcript accumulation: visit_id -> text
TRANSCRIPTS = {}


def _to_safe_visit_id(v):
    # avoid path traversal / weird filenames
    v = str(v)
    return "".join(ch for ch in v if ch.isalnum() or ch in ("-", "_"))[:64] or "unknown"


def _load_audio(path: str, target_sr: int = 16000) -> np.ndarray:
    """Load wav (or other formats supported by soundfile) -> mono float32 -> resample."""
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1).astype(np.float32)
    if sr != target_sr:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=target_sr).astype(np.float32)
    return audio


def get_asr():
    """Load processor + model once, reuse for chunk transcription."""
    if _ASR["model"] is not None:
        return _ASR["processor"], _ASR["model"]

    processor = WhisperProcessor.from_pretrained(BASE_MODEL_ID)

    torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32

    try:
        base = WhisperForConditionalGeneration.from_pretrained(
            BASE_MODEL_ID,
            device_map="auto",
            torch_dtype=torch_dtype,
        )
    except Exception:
        base = WhisperForConditionalGeneration.from_pretrained(
            BASE_MODEL_ID,
            torch_dtype=torch_dtype,
        )
        base = base.to("cuda" if torch.cuda.is_available() else "cpu")

    base.config.forced_decoder_ids = None
    base.config.suppress_tokens = []
    base.eval()

    model = base
    if USE_LORA and os.path.isdir(ADAPTER_DIR):
        try:
            model = PeftModel.from_pretrained(base, ADAPTER_DIR)
            model.eval()
        except Exception:
            model = base

    _ASR["processor"], _ASR["model"] = processor, model
    return processor, model


def transcribe_wav(path: str, language: str = None) -> str:
    processor, model = get_asr()
    audio = _load_audio(path, TARGET_SR)

    inputs = processor.feature_extractor(audio, sampling_rate=TARGET_SR, return_tensors="pt").input_features

    model_device = next(model.parameters()).device
    model_dtype = next(model.parameters()).dtype
    inputs = inputs.to(model_device).to(model_dtype)

    gen_kwargs = dict(
        input_features=inputs,
        max_new_tokens=256,
        task="transcribe",
    )
    if language:
        gen_kwargs["language"] = language

    with torch.no_grad():
        pred_ids = model.generate(**gen_kwargs)

    text = processor.tokenizer.decode(pred_ids[0], skip_special_tokens=True).strip()
    return text


def _save_transcript_to_file(visit_id: str, text: str):
    vid = _to_safe_visit_id(visit_id)
    fp = os.path.join(INSTANCE_FOLDER, f"visit_{vid}_transcript.txt")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(text)


# --- MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), default="away")
    room = db.Column(db.String(10), nullable=True)


class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ic_number = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.String(10), nullable=False)
    visits = db.relationship("Visit", backref="patient", lazy=True)


class Visit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    assigned_doctor = db.relationship("User", foreign_keys=[doctor_id])

    timestamp = db.Column(db.DateTime, default=datetime.now)
    symptoms = db.Column(db.Text, nullable=False)
    soap_note = db.Column(db.Text)
    status = db.Column(db.String(20), default="waiting")
    room = db.Column(db.String(10), nullable=True)


# --- AUTH ROUTES ---
@app.route("/")
def index():
    if "user_id" in session:
        return redirect("/doctor/dashboard") if session["role"] == "doctor" else redirect("/nurse/dashboard")
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        selected_role = request.form.get("role_selector")
        user = User.query.filter_by(email=request.form["email"]).first()
        
        if user and check_password_hash(user.password_hash, request.form["password"]):
            if user.role != selected_role:
                flash(f"Login Failed: You selected '{selected_role.capitalize()}' but this account belongs to a '{user.role.capitalize()}'.", "error")
                return render_template("login.html")

            session.permanent = True
            session["user_id"] = user.id
            session["role"] = user.role
            session["name"] = user.name

            if user.role == "doctor":
                user.status = "online"
                selected_room = request.form.get("room")
                if selected_room:
                    occupant = User.query.filter_by(room=selected_room, role='doctor', status='online').first()
                    if occupant and occupant.id != user.id:
                        flash(f'Room {selected_room} is occupied by {occupant.name}.', 'error')
                        return render_template('login.html')
                    user.room = selected_room
                db.session.commit()

            return redirect("/")
        flash("Invalid credentials", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    if "user_id" in session:
        user = User.query.get(session["user_id"])
        if user and user.role == "doctor":
            user.status = "away"
            user.room = None
            db.session.commit()
    session.clear()
    return redirect("/login")


# --- NURSE ROUTES ---
@app.route("/nurse/dashboard", methods=["GET", "POST"])
def nurse_dashboard():
    if "user_id" not in session or session.get("role") != "nurse":
        return redirect(url_for("login"))

    found_patient = None

    if request.method == "POST":
        action = request.form.get("action")

        def find_available_room():
            for r in range(1, 11):
                room_num = str(r)
                # Check Doctor
                doc = User.query.filter_by(role='doctor', room=room_num, status='online').first()
                if not doc: continue

                # Allow adding to queue even if there is a waiting patient (Basic Queue)
                # But strictly check for "in_consultation" to avoid double booking active session
                active_visit = Visit.query.filter(
                    Visit.room == room_num,
                    Visit.status == "in_consultation"
                ).first()

                if not active_visit:
                    return room_num
            return None

        if action == "register_new":
            name = request.form["name"]
            ic = request.form["ic"]
            age = request.form["age"]
            symptom_text = request.form["symptom"]

            if Patient.query.filter_by(ic_number=ic).first():
                flash("Patient with this IC already exists!", "error")
            else:
                new_patient = Patient(name=name, ic_number=ic, age=age)
                db.session.add(new_patient)
                db.session.flush()

                assigned_room = find_available_room()
                visit_status = "waiting" if assigned_room else "queued"

                new_visit = Visit(
                    patient_id=new_patient.id,
                    status=visit_status,
                    symptoms=symptom_text,
                    room=assigned_room,
                )
                db.session.add(new_visit)
                db.session.commit()

                if assigned_room:
                    flash(f"Patient registered and assigned to Room {assigned_room}!", "success")
                else:
                    flash("Patient registered. Added to waiting list (All doctors busy).", "warning")

        elif action == "search_patient":
            search_ic = request.form.get("search_ic")
            found_patient = Patient.query.filter_by(ic_number=search_ic).first()
            if not found_patient:
                flash("Patient not found.", "error")

        elif action == "add_existing_to_queue":
            patient_id = request.form.get("patient_id")
            symptom_text = request.form.get("symptom")

            assigned_room = find_available_room()
            visit_status = "waiting" if assigned_room else "queued"

            new_visit = Visit(
                patient_id=patient_id,
                status=visit_status,
                symptoms=symptom_text,
                room=assigned_room,
            )
            db.session.add(new_visit)
            db.session.commit()

            if assigned_room:
                flash(f"Patient assigned to Room {assigned_room}!", "success")
            else:
                flash("All rooms full. Patient added to waiting list.", "warning")
            return redirect(url_for("nurse_dashboard"))

    # === PREPARE ROOM DATA WITH NEW COLOR LOGIC ===
    rooms_data = []
    for i in range(1, 11):
        r_num = str(i)
        doc = User.query.filter_by(role="doctor", room=r_num, status="online").first()
        
        # Check for Active Consultation (Priority)
        visit_consulting = Visit.query.filter_by(room=r_num, status="in_consultation").first()
        # Check for Waiting Patient
        visit_waiting = Visit.query.filter_by(room=r_num, status="waiting").first()
        
        visit = visit_consulting if visit_consulting else visit_waiting
        
        # Logic: 
        # 1. No Doctor -> Gray ("Unavailable")
        # 2. Doctor + In Consultation -> Orange ("In Consultation")
        # 3. Doctor + (Empty OR Waiting) -> Green ("Open" / "Patient Waiting")
        
        if not doc:
            status_text = "Unavailable"
            color_class = "border-gray-300 opacity-75" # Gray / Dimmed
            dot_color = "bg-gray-400"
            text_color = "text-gray-500"
        else:
            if visit_consulting:
                status_text = "In Consultation"
                color_class = "border-orange-400" # Orange
                dot_color = "bg-orange-500"
                text_color = "text-orange-600"
            else:
                # Green for BOTH empty and waiting (as requested)
                status_text = "Patient Waiting" if visit_waiting else "Open"
                color_class = "border-green-400" # Green
                dot_color = "bg-green-500 animate-pulse"
                text_color = "text-green-600"

        rooms_data.append(
            {
                "number": r_num,
                "color_class": color_class,
                "dot_color": dot_color,
                "text_color": text_color,
                "status_text": status_text,
                "doctor_name": doc.name if doc else "No Doctor",
                "patient_name": visit.patient.name if visit else "Empty",
                "patient_ic": visit.patient.ic_number if visit else "",
                "patient_age": visit.patient.age if visit else "",
                "visit_symptoms": visit.symptoms if visit else "",
                "has_doctor": bool(doc),
                "has_patient": bool(visit)
            }
        )

    waiting_list = Visit.query.filter_by(status="queued").order_by(Visit.timestamp.desc()).all()

    return render_template(
        "nurse_dashboard.html",
        rooms=rooms_data,
        queue=waiting_list,
        found_patient=found_patient,
    )

# --- MISSING ROUTE RESTORED ---
@app.route('/nurse/room/<room_num>')
def view_room_details(room_num):
    if session.get('role') != 'nurse': return redirect('/login')
    
    doctor = User.query.filter_by(role='doctor', room=room_num, status='online').first()
    active_visit = Visit.query.filter(
        Visit.room == room_num, 
        Visit.status.in_(['waiting', 'in_consultation'])
    ).first()

    total_patients = 1 if active_visit else 0
    
    return render_template('nurse_room_details.html', 
                         room_num=room_num, 
                         doctor=doctor, 
                         active_visit=active_visit, 
                         total_patients=total_patients)


@app.route("/nurse/patient_list")
def nurse_patient_list():
    if session.get("role") != "nurse":
        return redirect("/login")
    patients = Patient.query.order_by(Patient.name.asc()).all()
    return render_template("nurse_patient_list.html", patients=patients)


@app.route("/nurse/view_patient/<ic>")
def view_patient_page(ic):
    p = Patient.query.filter_by(ic_number=ic).first_or_404()
    return render_template("nurse_patient_view.html", patient=p)


@app.route("/nurse/update_patient", methods=["POST"])
def update_patient():
    data = request.json
    p = Patient.query.filter_by(ic_number=data["ic"]).first_or_404()
    p.name = data["name"]
    p.age = data["age"]
    db.session.commit()
    return jsonify({"success": True})


@app.route("/nurse/delete_patient", methods=["POST"])
def delete_patient():
    data = request.json
    p = Patient.query.filter_by(ic_number=data["ic"]).first_or_404()
    Visit.query.filter_by(patient_id=p.id).delete()
    db.session.delete(p)
    db.session.commit()
    return jsonify({"success": True})

@app.route('/verify_doctor_id', methods=['POST'])
def verify_doctor_id():
    data = request.json
    doc_id = data.get('doctor_id')
    doctor = User.query.filter_by(id=doc_id, role='doctor').first()
    if doctor:
        return jsonify({'success': True, 'doctor_name': doctor.name})
    else:
        return jsonify({'success': False})

# --- DOCTOR ROUTES ---
@app.route("/doctor/dashboard")
def doctor_dashboard():
    if session.get("role") != "doctor":
        return redirect("/login")

    doctor = User.query.get(session["user_id"])
    my_room = doctor.room

    if my_room:
        queue = Visit.query.filter_by(room=my_room, status="waiting").order_by(Visit.timestamp.asc()).all()
    else:
        queue = []

    return render_template(
        "doctor_patients.html",
        patients=queue,
        doctor_name=doctor.name,
        doctor_status=doctor.status,
        current_user_id=doctor.id,
        current_room=my_room,
    )


@app.route("/doctor/history")
def doctor_history_page():
    return render_template("doctor_history.html", doctor_name=session.get("name"))


@app.route("/doctor/toggle_status", methods=["POST"])
def toggle_status():
    if session.get("role") != "doctor":
        return jsonify({"error": "Unauthorized"}), 403
    user = User.query.get(session["user_id"])
    user.status = request.json.get("status", "away")
    db.session.commit()
    return jsonify({"success": True})


@app.route("/start_consultation/<int:visit_id>")
def start_consultation(visit_id):
    if session.get("role") != "doctor":
        return redirect("/login")
    visit = Visit.query.get_or_404(visit_id)

    visit.doctor_id = session["user_id"]
    db.session.commit()

    TRANSCRIPTS[str(visit.id)] = ""

    return render_template("consultation.html", visit=visit, patient=visit.patient, doctor_name=session["name"])


@app.route("/doctor/demo_session")
def demo_session():
    if session.get("role") != "doctor":
        return redirect("/login")

    class MockPatient:
        name = "TEST PATIENT (DEMO)"
        ic_number = "000000-00-0000"
        age = "99"

    class MockVisit:
        id = "demo"
        symptoms = "Self-Test Mode: No real patient. Testing microphone and AI transcription."

    TRANSCRIPTS["demo"] = ""

    return render_template("consultation.html", visit=MockVisit(), patient=MockPatient(), doctor_name=session["name"])


@app.route("/process_audio", methods=["POST"])
def process_audio():
    if "audio_data" not in request.files:
        return jsonify({"error": "No audio file"}), 400

    audio_file = request.files["audio_data"]
    visit_id = request.form.get("visit_id", "demo")
    chunk_idx = request.form.get("chunk_idx", "0")
    is_final = request.form.get("final", "0") == "1"

    safe_vid = _to_safe_visit_id(visit_id)
    ts = int(time.time())

    filename = f"visit_{safe_vid}chunk{chunk_idx}_{ts}.wav"
    save_path = os.path.join(INSTANCE_FOLDER, filename)
    audio_file.save(save_path)

    try:
        chunk_text = transcribe_wav(save_path, language=None)
    except Exception as e:
        return jsonify({"error": f"ASR failed: {str(e)}"}), 500

    key = str(visit_id)
    prev = TRANSCRIPTS.get(key, "")
    full = (prev + " " + chunk_text).strip() if prev else chunk_text
    TRANSCRIPTS[key] = full

    try:
        _save_transcript_to_file(visit_id, full)
    except Exception:
        pass

    return jsonify(
        {
            "transcription": chunk_text,
            "full_transcript": full,
            "final": is_final,
            "soap_note": "",
        }
    )


@app.route("/save_consultation", methods=["POST"])
def save_consultation():
    data = request.json
    visit_id = data.get("visit_id")
    if visit_id == "demo":
        demo_note = data.get("note", "")
        try:
            fp = os.path.join(INSTANCE_FOLDER, "demo_soap_note.txt")
            with open(fp, "w", encoding="utf-8") as f:
                f.write(demo_note)
        except Exception:
            pass
        return jsonify({"status": "success", "message": "Demo note processed"})

    visit = Visit.query.get(visit_id)
    if not visit:
        return jsonify({"error": "Visit not found"}), 404

    visit.soap_note = data.get("note")

    if data.get("action") == "finalize":
        visit.status = "completed"

    db.session.commit()
    return jsonify({"status": "success"})


@app.route("/patient/history/<ic>")
def get_patient_history(ic):
    p = Patient.query.filter_by(ic_number=ic).first()
    if not p:
        return jsonify([])
    history = [
        {
            "id": v.id,
            "date": v.timestamp.strftime("%Y-%m-%d %H:%M"),
            "symptoms": v.symptoms,
            "status": v.status,
            "note": v.soap_note or "No notes",
        }
        for v in p.visits
    ]
    history.reverse()
    return jsonify(history)


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        
        # 1. Create Nurse
        if not User.query.filter_by(email='nurse@test.com').first():
            db.session.add(User(name="Nurse Joy", email='nurse@test.com', password_hash=generate_password_hash('nurse123'), role='nurse'))
            
        # 2. Create Dr. Strange (ROOM 1, ONLINE)
        if not User.query.filter_by(email='doctor@test.com').first():
            db.session.add(User(name="Dr. Strange", email='doctor@test.com', password_hash=generate_password_hash('doctor123'), role='doctor', status='online', room='1'))
        else:
            dr = User.query.filter_by(email='doctor@test.com').first()
            dr.status = 'online'
            dr.room = '1'
            db.session.add(dr)

        # 3. Create Other Doctors with UNIQUE CREDENTIALS
        sim_doctors = [
            {'name': 'Dr. Jackson Wang', 'email': 'jackson@hospital.com', 'password': 'jackson123', 'room': '3'},
            {'name': 'Dr. Taylor Swift', 'email': 'taylor@hospital.com',  'password': 'taylor123',  'room': '8'},
            {'name': 'Dr. Aida Alya',    'email': 'aida@hospital.com',    'password': 'aida123',    'room': '9'},
            {'name': 'Dr. Aiman Afiq',   'email': 'aiman@hospital.com',   'password': 'aiman123',   'room': '5'},
            {'name': 'Dr. Jayden Lim',   'email': 'jayden@hospital.com',  'password': 'jayden123',  'room': '10'},
        ]

        for doc_data in sim_doctors:
            if not User.query.filter_by(email=doc_data['email']).first():
                new_doc = User(
                    name=doc_data['name'], 
                    email=doc_data['email'], 
                    password_hash=generate_password_hash(doc_data['password']), 
                    role='doctor', 
                    status='online',
                    room=doc_data['room']
                )
                db.session.add(new_doc)
        db.session.commit()

        # 4. Create Simulated Patients
        sim_patients = [
            # PRESENTATION PATIENT FOR DR STRANGE (ROOM 1)
            {'name': 'Presentation Demo Patient', 'ic': '999999-01-0001', 'age': '30', 'room': '1'},
            
            # PATIENTS FOR SIMULATED DOCTORS
            {'name': 'Bambi Lee', 'ic': '120820050506', 'age': '14', 'room': '3'},
            {'name': 'Nikola Tesla', 'ic': '120920050506', 'age': '14', 'room': '8'},
            {'name': 'Tong Shen Sheng', 'ic': '05040302010506', 'age': '20', 'room': '9'},
        ]

        for p_data in sim_patients:
            patient = Patient.query.filter_by(ic_number=p_data['ic']).first()
            if not patient:
                patient = Patient(name=p_data['name'], ic_number=p_data['ic'], age=p_data['age'])
                db.session.add(patient)
                db.session.commit()
            
            # Create a waiting visit if none exists
            active_visit = Visit.query.filter_by(room=p_data['room'], status='waiting').first()
            if not active_visit:
                room_doc = User.query.filter_by(room=p_data['room'], role='doctor').first()
                new_visit = Visit(
                    patient_id=patient.id,
                    doctor_id=room_doc.id if room_doc else None,
                    symptoms="Cough and fever for 3 days.",
                    status='waiting', 
                    room=p_data['room']
                )
                db.session.add(new_visit)
        
        db.session.commit()
        print(">>> Simulation Data Loaded. All Doctors and Patients ready.")

    app.run(host='0.0.0.0', port=5000, debug=False)