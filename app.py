from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta, datetime
import os
import random

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- CONFIGURATION ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_FOLDER = os.path.join(BASE_DIR, 'instance')
if not os.path.exists(INSTANCE_FOLDER):
    os.makedirs(INSTANCE_FOLDER)

db_path = os.path.join(INSTANCE_FOLDER, 'medical_scribe.db')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.permanent_session_lifetime = timedelta(minutes=60)
db = SQLAlchemy(app)

# --- MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), default='away') 
    room = db.Column(db.String(10), nullable=True)

class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ic_number = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.String(10), nullable=False)
    visits = db.relationship('Visit', backref='patient', lazy=True)

class Visit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) 
    assigned_doctor = db.relationship('User', foreign_keys=[doctor_id]) 
    
    timestamp = db.Column(db.DateTime, default=datetime.now)
    symptoms = db.Column(db.Text, nullable=False)
    soap_note = db.Column(db.Text)
    status = db.Column(db.String(20), default='waiting')
    room = db.Column(db.String(10), nullable=True) 

# --- AUTH ROUTES ---
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect('/doctor/dashboard') if session['role'] == 'doctor' else redirect('/nurse/dashboard')
    return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form['email']).first()
        if user and check_password_hash(user.password_hash, request.form['password']):
            session.permanent = True
            session['user_id'] = user.id
            session['role'] = user.role
            session['name'] = user.name
            
            if user.role == 'doctor':
                user.status = 'online'
                selected_room = request.form.get('room')
                if selected_room:
                    user.room = selected_room
                db.session.commit()
            
            return redirect('/')
        flash('Invalid credentials', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        if user and user.role == 'doctor':
            user.status = 'away'
            user.room = None 
            db.session.commit()
    session.clear()
    return redirect('/login')

# --- NURSE ROUTES ---
@app.route('/nurse/dashboard', methods=['GET', 'POST'])
def nurse_dashboard():
    if 'user_id' not in session or session.get('role') != 'nurse':
        return redirect(url_for('login'))

    found_patient = None 
    
    if request.method == 'POST':
        action = request.form.get('action')

        # === UPDATED LOGIC: FIND ROOM WITH AVAILABLE DOCTOR ===
        def find_available_room():
            # Iterate through all rooms
            for r in range(1, 11):
                room_num = str(r)
                
                # 1. Check if Doctor is ONLINE in this room
                doctor = User.query.filter_by(role='doctor', room=room_num, status='online').first()
                if not doctor:
                    continue # Skip if no doctor or doctor is away
                
                # 2. Check if Room is FREE (no active patient)
                active_visit = Visit.query.filter(
                    Visit.room == room_num, 
                    Visit.status.in_(['waiting', 'in_consultation'])
                ).first()
                
                if not active_visit:
                    return room_num # Found a room with a doctor and no patient!
            
            return None # No suitable rooms found

        # --- ACTION 1: REGISTER NEW PATIENT ---
        if action == 'register_new':
            name = request.form['name']
            ic = request.form['ic']
            age = request.form['age']
            symptom_text = request.form['symptom'] 
            
            if Patient.query.filter_by(ic_number=ic).first():
                flash('Patient with this IC already exists!', 'error')
            else:
                new_patient = Patient(name=name, ic_number=ic, age=age)
                db.session.add(new_patient)
                db.session.flush()

                assigned_room = find_available_room()
                visit_status = 'waiting' if assigned_room else 'queued'
                
                new_visit = Visit(
                    patient_id=new_patient.id, 
                    status=visit_status,
                    symptoms=symptom_text,
                    room=assigned_room
                )
                db.session.add(new_visit)
                db.session.commit()
                
                if assigned_room:
                    flash(f'Patient registered and assigned to Room {assigned_room}!', 'success')
                else:
                    flash('Patient registered. Added to Waiting List (No Available Rooms with Doctors).', 'warning')

        # --- ACTION 2: SEARCH PATIENT ---
        elif action == 'search_patient':
            search_ic = request.form.get('search_ic')
            found_patient = Patient.query.filter_by(ic_number=search_ic).first()
            if not found_patient: flash('Patient not found.', 'error')

        # --- ACTION 3: ADD EXISTING PATIENT ---
        elif action == 'add_existing_to_queue':
            patient_id = request.form.get('patient_id')
            symptom_text = request.form.get('symptom')
            p_obj = Patient.query.get(patient_id)

            assigned_room = find_available_room()
            visit_status = 'waiting' if assigned_room else 'queued'

            new_visit = Visit(
                patient_id=patient_id, 
                status=visit_status, 
                symptoms=symptom_text,
                room=assigned_room
            )
            db.session.add(new_visit)
            db.session.commit()
            
            if assigned_room:
                flash(f'{p_obj.name} assigned to Room {assigned_room}', 'success')
            else:
                flash(f'{p_obj.name} added to Waiting List (No Available Rooms with Doctors).', 'warning')
            return redirect(url_for('nurse_dashboard'))

    # === PREPARE DASHBOARD DATA ===
    rooms_data = []
    for i in range(1, 11):
        r_num = str(i)
        doc = User.query.filter_by(role='doctor', room=r_num, status='online').first()
        visit = Visit.query.filter(
            Visit.room == r_num, 
            Visit.status.in_(['waiting', 'in_consultation'])
        ).first()
        
        status_color = 'orange' if visit else ('green' if doc else 'gray') # Gray if no doctor
        
        rooms_data.append({
            'number': r_num,
            'color': status_color,
            'doctor_name': doc.name if doc else "No Doctor Available",
            'patient_name': visit.patient.name if visit else "Empty",
            'patient_ic': visit.patient.ic_number if visit else "",
            'patient_age': visit.patient.age if visit else "",
            'visit_symptoms': visit.symptoms if visit else "",
            'is_free': not visit,
            'has_doctor': bool(doc)
        })

    waiting_list = Visit.query.filter_by(status='queued').order_by(Visit.timestamp.desc()).all()
    
    return render_template('nurse_dashboard.html', rooms=rooms_data, queue=waiting_list, found_patient=found_patient)

# --- NEW ROUTE: INDIVIDUAL ROOM DETAILS ---
@app.route('/nurse/room/<room_num>')
def view_room_details(room_num):
    if session.get('role') != 'nurse': return redirect('/login')
    
    # 1. Get Doctor Info
    doctor = User.query.filter_by(role='doctor', room=room_num, status='online').first()
    
    # 2. Get Active Patient (if any)
    active_visit = Visit.query.filter(
        Visit.room == room_num, 
        Visit.status.in_(['waiting', 'in_consultation'])
    ).first()

    # 3. Get Total Count for this room (For now, usually 1 active + maybe others if we had room-specific queues)
    # Since logic only assigns 1 at a time, count is 1 or 0.
    total_patients = 1 if active_visit else 0
    
    return render_template('nurse_room_details.html', 
                         room_num=room_num, 
                         doctor=doctor, 
                         active_visit=active_visit, 
                         total_patients=total_patients)

@app.route('/nurse/patient_list')
def nurse_patient_list():
    if session.get('role') != 'nurse': return redirect('/login')
    patients = Patient.query.order_by(Patient.name.asc()).all()
    return render_template('nurse_patient_list.html', patients=patients)

@app.route('/nurse/view_patient/<ic>')
def view_patient_page(ic):
    p = Patient.query.filter_by(ic_number=ic).first_or_404()
    return render_template('nurse_patient_view.html', patient=p)

@app.route('/nurse/update_patient', methods=['POST'])
def update_patient():
    data = request.json
    p = Patient.query.filter_by(ic_number=data['ic']).first_or_404()
    p.name = data['name']; p.age = data['age']
    db.session.commit()
    return jsonify({'success': True})

@app.route('/nurse/delete_patient', methods=['POST'])
def delete_patient():
    data = request.json
    p = Patient.query.filter_by(ic_number=data['ic']).first_or_404()
    Visit.query.filter_by(patient_id=p.id).delete()
    db.session.delete(p)
    db.session.commit()
    return jsonify({'success': True})

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
@app.route('/doctor/dashboard')
def doctor_dashboard():
    if session.get('role') != 'doctor': return redirect('/login')
    doctor = User.query.get(session['user_id'])
    my_room = doctor.room
    if my_room:
        queue = Visit.query.filter_by(room=my_room, status='waiting').order_by(Visit.timestamp.asc()).all()
    else:
        queue = []
    return render_template('doctor_patients.html', patients=queue, doctor_name=doctor.name, doctor_status=doctor.status, current_user_id=doctor.id, current_room=my_room)

@app.route('/doctor/history')
def doctor_history_page():
    return render_template('doctor_history.html', doctor_name=session.get('name'))

@app.route('/doctor/toggle_status', methods=['POST'])
def toggle_status():
    if session.get('role') != 'doctor': return jsonify({'error': 'Unauthorized'}), 403
    user = User.query.get(session['user_id'])
    user.status = request.json.get('status', 'away')
    db.session.commit()
    return jsonify({'success': True})

@app.route('/start_consultation/<int:visit_id>')
def start_consultation(visit_id):
    if session.get('role') != 'doctor': return redirect('/login')
    visit = Visit.query.get_or_404(visit_id)
    visit.doctor_id = session['user_id']
    db.session.commit()
    return render_template('consultation.html', visit=visit, patient=visit.patient, doctor_name=session['name'])

# --- DEMO ROUTES ---
@app.route('/doctor/demo_session')
def demo_session():
    if session.get('role') != 'doctor': return redirect('/login')
    class MockPatient:
        name = "TEST PATIENT (DEMO)"
        ic_number = "000000-00-0000"
        age = "99"
    class MockVisit:
        id = "demo"
        symptoms = "Self-Test Mode: No real patient. Testing microphone and AI transcription."
    return render_template('consultation.html', visit=MockVisit(), patient=MockPatient(), doctor_name=session['name'])

@app.route('/process_audio', methods=['POST'])
def process_audio():
    if 'audio_data' not in request.files: return jsonify({'error': 'No audio file'}), 400
    audio_file = request.files['audio_data']
    visit_id = request.form.get('visit_id')
    filename = f"visit_{visit_id}.wav"
    save_path = os.path.join(INSTANCE_FOLDER, filename)
    audio_file.save(save_path)
    text = "(DEMO) This is a test transcription. The audio was received successfully."
    soap = "S: Testing\nO: Audio Clear\nA: System Functional\nP: Continue Deployment"
    return jsonify({'transcription': text, 'soap_note': soap})

@app.route('/save_consultation', methods=['POST'])
def save_consultation():
    data = request.json
    visit_id = data.get('visit_id')
    if visit_id == 'demo': return jsonify({'status': 'success', 'message': 'Demo note processed'})
    visit = Visit.query.get(visit_id)
    if not visit: return jsonify({'error': 'Visit not found'}), 404
    visit.soap_note = data.get('note')
    if data.get('action') == 'finalize': 
        visit.status = 'completed'
    db.session.commit()
    return jsonify({'status': 'success'})

@app.route('/patient/history/<ic>')
def get_patient_history(ic):
    p = Patient.query.filter_by(ic_number=ic).first()
    if not p: return jsonify([])
    history = [{'date': v.timestamp.strftime('%Y-%m-%d %H:%M'), 'symptoms': v.symptoms, 'status': v.status, 'note': v.soap_note or "No notes"} for v in p.visits]
    history.reverse()
    return jsonify(history)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # --- SIMULATION DATA ---
        if not User.query.filter_by(email='nurse@test.com').first():
            db.session.add(User(name="Nurse Joy", email='nurse@test.com', password_hash=generate_password_hash('nurse123'), role='nurse'))
            
        sim_doctors = [
            {'name': 'Dr. Jackson Wang', 'email': 'jacksonwang@hospital.com', 'room': '3'},
            {'name': 'Dr. Taylor Swift', 'email': 'taylorswift@hospital.com', 'room': '8'},
            {'name': 'Dr. Aida Alya', 'email': 'aidaalya@hospital.com', 'room': '9'},
            {'name': 'Dr. Aiman Afiq', 'email': 'aimanafiq@hospital.com', 'room': '5'},
            {'name': 'Dr. Jayden Lim', 'email': 'lim@hospital.com', 'room': '10'},
        ]
        for doc_data in sim_doctors:
            if not User.query.filter_by(email=doc_data['email']).first():
                new_doc = User(name=doc_data['name'], email=doc_data['email'], password_hash=generate_password_hash('password'), role='doctor', status='online', room=doc_data['room'])
                db.session.add(new_doc)
        db.session.commit()
    app.run(debug=True)