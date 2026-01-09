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
    room = db.Column(db.String(10), nullable=True) # NEW: Assigned Room

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
            
            # --- DOCTOR ROOM LOGIC ---
            if user.role == 'doctor':
                user.status = 'online'
                # The form sends 'room' only if visible, but we check if it exists
                selected_room = request.form.get('room')
                if selected_room:
                    user.room = selected_room
                db.session.commit()
            # -------------------------
            
            return redirect('/')
        flash('Invalid credentials', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        if user and user.role == 'doctor':
            user.status = 'away'
            user.room = None # Clear room on logout
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

        # === SHARED LOGIC: FIND AVAILABLE ROOM ===
        def find_available_room():
            # Strategy: Find a room that has NO active visit (waiting or in_consultation)
            # Simulation Mode: All 10 rooms are "available" if empty, regardless of doctor presence
            for r in range(1, 11):
                room_num = str(r)
                # Check if this room is occupied by a patient
                active_visit = Visit.query.filter(
                    Visit.room == room_num, 
                    Visit.status.in_(['waiting', 'in_consultation'])
                ).first()
                
                if not active_visit:
                    return room_num
            return None # All rooms full

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

                # AUTO ASSIGN ROOM
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
                    flash('Patient registered but all rooms are full. Added to waiting list.', 'warning')

        # --- ACTION 2: SEARCH PATIENT ---
        elif action == 'search_patient':
            search_ic = request.form.get('search_ic')
            found_patient = Patient.query.filter_by(ic_number=search_ic).first()
            if not found_patient: flash('Patient not found.', 'error')
            else: flash(f'Patient found: {found_patient.name}', 'success')

        # --- ACTION 3: ADD EXISTING PATIENT ---
        elif action == 'add_existing_to_queue':
            patient_id = request.form.get('patient_id')
            symptom_text = request.form.get('symptom')
            
            # AUTO ASSIGN ROOM
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
                flash(f'Patient assigned to Room {assigned_room}!', 'success')
            else:
                flash('All rooms full. Patient added to waiting list.', 'warning')
            return redirect(url_for('nurse_dashboard'))

    # === PREPARE DASHBOARD DATA (ROOM GRID) ===
    rooms_data = []
    for i in range(1, 11):
        r_num = str(i)
        # Find Doctor in this room
        doc = User.query.filter_by(role='doctor', room=r_num, status='online').first()
        # Find Patient in this room
        visit = Visit.query.filter(
            Visit.room == r_num, 
            Visit.status.in_(['waiting', 'in_consultation'])
        ).first()
        
        status_color = 'orange' if visit else 'green' # Orange = Occupied, Green = Free
        
        rooms_data.append({
            'number': r_num,
            'color': status_color,
            'doctor_name': doc.name if doc else "No Doctor",
            'patient_name': visit.patient.name if visit else "Empty",
            'is_free': not visit
        })

    # Fetch Waiting List (Patients not assigned to a room yet)
    waiting_list = Visit.query.filter_by(status='queued').order_by(Visit.timestamp.desc()).all()
    
    return render_template('nurse_dashboard.html', rooms=rooms_data, queue=waiting_list, found_patient=found_patient)

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

# --- DOCTOR ROUTES ---
@app.route('/doctor/dashboard')
def doctor_dashboard():
    if session.get('role') != 'doctor': return redirect('/login')
    
    doctor = User.query.get(session['user_id'])
    my_room = doctor.room
    
    # Only show patients assigned to THIS doctor's room
    if my_room:
        queue = Visit.query.filter_by(room=my_room, status='waiting').order_by(Visit.timestamp.asc()).all()
    else:
        queue = []

    return render_template('doctor_patients.html', 
                         patients=queue, 
                         doctor_name=doctor.name, 
                         doctor_status=doctor.status, 
                         current_user_id=doctor.id,
                         current_room=my_room)

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
    
    # Assign Doctor ID to visit when they actually start
    visit.doctor_id = session['user_id']
      
    # Commit the doctor_id assignment
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
    
    # When completed, free up the room!
    if data.get('action') == 'finalize': 
        visit.status = 'completed'
        # room logic is handled by 'status' check in dashboard (completed visits don't block rooms)
        
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
        
        # --- SIMULATION DATA SETUP ---
        
        # 1. Create Default Nurse & General Doctor
        if not User.query.filter_by(email='nurse@test.com').first():
            db.session.add(User(name="Nurse Joy", email='nurse@test.com', password_hash=generate_password_hash('nurse123'), role='nurse'))
            
        if not User.query.filter_by(email='doctor@test.com').first():
            db.session.add(User(name="Dr. Strange", email='doctor@test.com', password_hash=generate_password_hash('doctor123'), role='doctor', status='away'))

        # 2. Simulation: Doctors in specific rooms
        sim_doctors = [
            # Room 3 (Occupied with Patient)
            {'name': 'Dr. Jackson Wang', 'email': 'jacksonwang@hospital.com', 'room': '3', 'status': 'online'},
            # Room 8 (Occupied with Patient)
            {'name': 'Dr. Taylor Swift', 'email': 'taylorswift@hospital.com', 'room': '8', 'status': 'online'},
            # Room 9 (Occupied with Patient)
            {'name': 'Dr. Aida Alya', 'email': 'aidaalya@hospital.com', 'room': '9', 'status': 'online'},
            # Room 5 (Doctor Only - No Patient)
            {'name': 'Dr. Aiman Afiq', 'email': 'aimanafiq@hospital.com', 'room': '5', 'status': 'online'},
            # Room 10 (Doctor Only - No Patient)
            {'name': 'Dr. Jayden Lim', 'email': 'lim@hospital.com', 'room': '10', 'status': 'online'},
        ]

        for doc_data in sim_doctors:
            if not User.query.filter_by(email=doc_data['email']).first():
                new_doc = User(
                    name=doc_data['name'], 
                    email=doc_data['email'], 
                    password_hash=generate_password_hash('password'), 
                    role='doctor', 
                    status=doc_data['status'],
                    room=doc_data['room']
                )
                db.session.add(new_doc)
        db.session.commit()

        # 3. Simulation: Patients for Rooms 3, 8, 9
        sim_patients = [
            {'name': 'Bambi Lee', 'ic': '120820050506', 'age': '14', 'room': '3'},
            {'name': 'Nikola Tesla', 'ic': '120920050506', 'age': '14', 'room': '8'},
            {'name': 'Tong Shen Sheng', 'ic': '05040302010506', 'age': '20', 'room': '9'},
        ]

        for p_data in sim_patients:
            # Create Patient if not exists
            patient = Patient.query.filter_by(ic_number=p_data['ic']).first()
            if not patient:
                patient = Patient(name=p_data['name'], ic_number=p_data['ic'], age=p_data['age'])
                db.session.add(patient)
                db.session.commit()
            
            # Create Active Visit (Marking Room Occupied)
            # We check if there's already an active visit in this room to avoid duplicates on restart
            active_visit = Visit.query.filter_by(room=p_data['room'], status='waiting').first()
            if not active_visit:
                # Find the doctor for this room to assign correctly
                room_doc = User.query.filter_by(room=p_data['room'], role='doctor').first()
                
                new_visit = Visit(
                    patient_id=patient.id,
                    doctor_id=room_doc.id if room_doc else None,
                    symptoms="Simulation: Severe headache and dizziness.",
                    status='waiting', # 'waiting' or 'in_consultation' makes it occupied
                    room=p_data['room']
                )
                db.session.add(new_visit)
        
        db.session.commit()
        print(">>> Simulation Data Loaded: Rooms 3, 8, 9 Occupied. Rooms 5, 10 Doctor Ready.")

    app.run(debug=True)