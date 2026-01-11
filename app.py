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
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

db = SQLAlchemy(app)

# ===== ASR CONFIG (Unchanged) =====
BASE_MODEL_ID = "mesolitica/malaysian-whisper-medium-v2"
ADAPTER_DIR = "rojak_medium_lora_adapter"
USE_LORA = True
TARGET_SR = 16000

_ASR = {"processor": None, "model": None}
TRANSCRIPTS = {}

def _to_safe_visit_id(v):
    v = str(v)
    return "".join(ch for ch in v if ch.isalnum() or ch in ("-", "_"))[:64] or "unknown"

def _load_audio(path: str, target_sr: int = 16000) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1).astype(np.float32)
    if sr != target_sr:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=target_sr).astype(np.float32)
    return audio

def get_asr():
    if _ASR["model"] is not None:
        return _ASR["processor"], _ASR["model"]
    processor = WhisperProcessor.from_pretrained(BASE_MODEL_ID)
    torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32
    try:
        base = WhisperForConditionalGeneration.from_pretrained(BASE_MODEL_ID, device_map="auto", torch_dtype=torch_dtype)
    except Exception:
        base = WhisperForConditionalGeneration.from_pretrained(BASE_MODEL_ID, torch_dtype=torch_dtype)
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
    gen_kwargs = dict(input_features=inputs, max_new_tokens=256, task="transcribe")
    if language: gen_kwargs["language"] = language
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
                flash(f"Login Failed: Account role '{user.role}' does not match selection '{selected_role}'.", "error")
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

        # === UPDATED ALGORITHM: QUEUEING ALLOWED ===
        def find_available_room():
            # 1. Find all rooms with an ONLINE doctor
            online_docs = User.query.filter_by(role='doctor', status='online').all()
            if not online_docs:
                return None
            
            # 2. Filter out doctors who haven't selected a room yet
            valid_rooms = [d.room for d in online_docs if d.room]
            if not valid_rooms:
                return None

            # 3. Load Balancing: Pick the room with the fewest waiting patients
            best_room = None
            min_queue = 9999

            for r_num in valid_rooms:
                queue_count = Visit.query.filter_by(room=r_num, status='waiting').count()
                if queue_count < min_queue:
                    min_queue = queue_count
                    best_room = r_num
            
            return best_room

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
                    room=assigned_room
                )
                db.session.add(new_visit)
                db.session.commit()

                if assigned_room:
                    flash(f"Patient {name} assigned to Room {assigned_room}", "success")
                else:
                    flash("No online doctors. Added to general waiting list.", "warning")

        elif action == "search_patient":
            search_ic = request.form.get("search_ic")
            found_patient = Patient.query.filter_by(ic_number=search_ic).first()
            if not found_patient:
                flash("Patient not found.", "error")

        elif action == "add_existing_to_queue":
            patient_id = request.form.get("patient_id")
            symptom_text = request.form.get("symptom")
            p_obj = Patient.query.get(patient_id)

            assigned_room = find_available_room()
            visit_status = "waiting" if assigned_room else "queued"

            new_visit = Visit(
                patient_id=patient_id,
                status=visit_status,
                symptoms=symptom_text,
                room=assigned_room
            )
            db.session.add(new_visit)
            db.session.commit()

            if assigned_room:
                flash(f"{p_obj.name} assigned to Room {assigned_room}", "success")
            else:
                flash("No online doctors. Added to general waiting list.", "warning")
            return redirect(url_for("nurse_dashboard"))

    # === DASHBOARD DISPLAY LOGIC ===
    rooms_data = []
    for i in range(1, 11):
        r_num = str(i)
        doc = User.query.filter_by(role="doctor", room=r_num, status="online").first()
        
        # 1. Check for Active Consultation
        visit_consulting = Visit.query.filter_by(room=r_num, status="in_consultation").first()
        
        # 2. Get ALL Waiting Patients (Queue)
        waiting_queue = Visit.query.filter_by(room=r_num, status="waiting").order_by(Visit.timestamp.asc()).all()
        
        # Determine Main Patient Name to Show
        # Priority: Consulting -> First in Queue -> Empty
        main_visit = visit_consulting if visit_consulting else (waiting_queue[0] if waiting_queue else None)

        # Logic for Colors & Text
        if not doc:
            # DOCTOR OFFLINE
            status_text = "Unavailable"
            color_class = "border-gray-300 opacity-75"
            dot_color = "bg-gray-400"
            text_color = "text-gray-500"
            next_in_queue_name = None
        
        elif visit_consulting:
            # IN CONSULTATION (Orange)
            status_text = "In Consultation"
            color_class = "border-orange-400"
            dot_color = "bg-orange-500"
            text_color = "text-orange-600"
            # If there's ALSO a queue, show the first person waiting
            next_in_queue_name = waiting_queue[0].patient.name if waiting_queue else None

        else:
            # DOCTOR ONLINE (Green)
            # Even if patient is waiting, it stays green (Ready to take patient)
            status_text = "Patient Waiting" if waiting_queue else "Open"
            color_class = "border-green-400"
            dot_color = "bg-green-500 animate-pulse"
            text_color = "text-green-600"
            
            # If > 1 patient waiting, or just 1 waiting?
            # User requirement: "if room got more than one patient... show Next in Queue in blue"
            # If we show the first patient as "main", the "next" is the second one?
            # Or does "more than one patient" mean "1 inside + others waiting"?
            
            # Implementation: 
            # If 1 Waiting -> Show as Main. Next is None.
            # If 2 Waiting -> Show 1st as Main. Show 2nd as "Next".
            next_in_queue_name = waiting_queue[1].patient.name if len(waiting_queue) > 1 else None

        rooms_data.append({
            "number": r_num,
            "color_class": color_class,
            "dot_color": dot_color,
            "text_color": text_color,
            "status_text": status_text,
            "doctor_name": doc.name if doc else "No Doctor",
            "patient_name": main_visit.patient.name if main_visit else "Empty",
            "patient_ic": main_visit.patient.ic_number if main_visit else "",
            "patient_age": main_visit.patient.age if main_visit else "",
            "visit_symptoms": main_visit.symptoms if main_visit else "",
            "has_doctor": bool(doc),
            "has_patient": bool(main_visit),
            "next_queue": next_in_queue_name # NEW FIELD
        })

    general_queue = Visit.query.filter_by(status="queued").order_by(Visit.timestamp.desc()).all()

    return render_template(
        "nurse_dashboard.html",
        rooms=rooms_data,
        queue=general_queue,
        found_patient=found_patient,
    )

@app.route('/nurse/room/<room_num>')
def view_room_details(room_num):
    if session.get('role') != 'nurse': return redirect('/login')
    
    # 1. Get Doctor Info
    doctor = User.query.filter_by(role='doctor', room=room_num, status='online').first()
    
    # 2. Get ALL visits associated with this room (Waiting, In Consultation, Completed)
    # Ordered by: Active/Waiting first, then by time.
    visits = Visit.query.filter(
        Visit.room == room_num,
        Visit.status.in_(['waiting', 'in_consultation', 'completed'])
    ).order_by(
        # Custom sort: In Consultation -> Waiting -> Completed
        db.case(
            (Visit.status == 'in_consultation', 1),
            (Visit.status == 'waiting', 2),
            (Visit.status == 'completed', 3),
            else_=4
        ),
        Visit.timestamp.asc()
    ).all()

    return render_template('nurse_room_details.html', 
                         room_num=room_num, 
                         doctor=doctor, 
                         visits=visits)

@app.route("/nurse/patients")
def nurse_patient_list():
    if session.get("role") != "nurse": return redirect("/login")
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
    p.name = data["name"]; p.age = data["age"]
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
    doctor = User.query.filter_by(id=data.get('doctor_id'), role='doctor').first()
    return jsonify({'success': True, 'doctor_name': doctor.name} if doctor else {'success': False})

# --- DOCTOR ROUTES ---
@app.route("/doctor/dashboard")
def doctor_dashboard():
    if session.get("role") != "doctor": return redirect("/login")
    doctor = User.query.get(session["user_id"])
    my_room = doctor.room
    # Fetch ALL waiting patients for this room
    queue = Visit.query.filter_by(room=my_room, status="waiting").order_by(Visit.timestamp.asc()).all() if my_room else []
    return render_template("doctor_patients.html", patients=queue, doctor_name=doctor.name, doctor_status=doctor.status, current_user_id=doctor.id, current_room=my_room)

@app.route("/doctor/history")
def doctor_history_page():
    return render_template("doctor_history.html", doctor_name=session.get("name"))

@app.route("/doctor/toggle_status", methods=["POST"])
def toggle_status():
    if session.get("role") != "doctor": return jsonify({"error": "Unauthorized"}), 403
    user = User.query.get(session["user_id"])
    user.status = request.json.get("status", "away")
    db.session.commit()
    return jsonify({"success": True})

@app.route("/start_consultation/<int:visit_id>")
def start_consultation(visit_id):
    if session.get("role") != "doctor": return redirect("/login")
    visit = Visit.query.get_or_404(visit_id)
    # Set status to in_consultation so nurse dashboard turns Orange
    visit.status = "in_consultation"
    visit.doctor_id = session["user_id"]
    db.session.commit()
    TRANSCRIPTS[str(visit.id)] = ""
    return render_template("consultation.html", visit=visit, patient=visit.patient, doctor_name=session["name"])

@app.route("/doctor/demo_session")
def demo_session():
    if session.get("role") != "doctor": return redirect("/login")
    class MockPatient: name="TEST PATIENT"; ic_number="000"; age="99"
    class MockVisit: id="demo"; symptoms="Test Mode"
    TRANSCRIPTS["demo"] = ""
    return render_template("consultation.html", visit=MockVisit(), patient=MockPatient(), doctor_name=session["name"])

@app.route("/process_audio", methods=["POST"])
def process_audio():
    if "audio_data" not in request.files: return jsonify({"error": "No file"}), 400
    f = request.files["audio_data"]
    vid = request.form.get("visit_id", "demo")
    idx = request.form.get("chunk_idx", "0")
    safe_vid = _to_safe_visit_id(vid)
    path = os.path.join(INSTANCE_FOLDER, f"visit_{safe_vid}_chunk{idx}.wav")
    f.save(path)
    try: text = transcribe_wav(path)
    except Exception as e: return jsonify({"error": str(e)}), 500
    full = (TRANSCRIPTS.get(str(vid), "") + " " + text).strip()
    TRANSCRIPTS[str(vid)] = full
    _save_transcript_to_file(vid, full)
    return jsonify({"transcription": text, "full_transcript": full})

@app.route("/save_consultation", methods=["POST"])
def save_consultation():
    data = request.json
    vid = data.get("visit_id")
    if vid == "demo": return jsonify({"status": "success"})
    visit = Visit.query.get(vid)
    if not visit: return jsonify({"error": "Not found"}), 404
    visit.soap_note = data.get("note")
    if data.get("action") == "finalize": visit.status = "completed"
    db.session.commit()
    return jsonify({"status": "success"})

@app.route("/patient/history/<ic>")
def get_patient_history(ic):
    p = Patient.query.filter_by(ic_number=ic).first()
    if not p: return jsonify([])
    history = [{"id": v.id, "date": v.timestamp.strftime("%Y-%m-%d"), "symptoms": v.symptoms, "status": v.status, "note": v.soap_note or "No notes"} for v in p.visits]
    return jsonify(history[::-1])

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(email='nurse@test.com').first():
            db.session.add(User(name="Nurse Joy", email='nurse@test.com', password_hash=generate_password_hash('nurse123'), role='nurse'))
        if not User.query.filter_by(email='doctor@test.com').first():
            db.session.add(User(name="Dr. Strange", email='doctor@test.com', password_hash=generate_password_hash('doctor123'), role='doctor', status='online', room='1'))
        
        sim_docs = [
            {'n':'Dr. Jackson', 'e':'jackson@hospital.com', 'p':'jackson123', 'r':'3'},
            {'n':'Dr. Taylor', 'e':'taylor@hospital.com', 'p':'taylor123', 'r':'8'},
            {'n':'Dr. Aida', 'e':'aida@hospital.com', 'p':'aida123', 'r':'9'},
            {'n':'Dr. Aiman', 'e':'aiman@hospital.com', 'p':'aiman123', 'r':'5'},
            {'n':'Dr. Jayden', 'e':'jayden@hospital.com', 'p':'jayden123', 'r':'10'}
        ]
        for d in sim_docs:
            if not User.query.filter_by(email=d['e']).first():
                db.session.add(User(name=d['n'], email=d['e'], password_hash=generate_password_hash(d['p']), role='doctor', status='online', room=d['r']))
        db.session.commit()
    app.run(host='0.0.0.0', port=5000, debug=False)