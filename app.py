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
    status = db.Column(db.String(20), default='away') # New: online/away

class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ic_number = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.String(10), nullable=False)
    visits = db.relationship('Visit', backref='patient', lazy=True)

class Visit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) # Assigned Doctor
    assigned_doctor = db.relationship('User', foreign_keys=[doctor_id]) 
    
    timestamp = db.Column(db.DateTime, default=datetime.now)
    symptoms = db.Column(db.Text, nullable=False)
    soap_note = db.Column(db.Text)
    status = db.Column(db.String(20), default='waiting')

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
            
            # Auto-set doctor to online
            if user.role == 'doctor':
                user.status = 'online'
                db.session.commit()
            return redirect('/')
        flash('Invalid credentials', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    # Set doctor to away on logout
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        if user and user.role == 'doctor':
            user.status = 'away'
            db.session.commit()
    session.clear()
    return redirect('/login')

# --- NURSE ROUTES ---
@app.route('/nurse/dashboard')
def nurse_dashboard():
    if session.get('role') != 'nurse': return redirect('/login')
    # Show active queue
    queue = Visit.query.filter(Visit.status.in_(['waiting', 'in_consultation'])).all()
    return render_template('nurse_dashboard.html', queue=queue)

@app.route('/nurse/patient_list')
def nurse_patient_list():
    if session.get('role') != 'nurse': return redirect('/login')
    visits = Visit.query.join(Patient).order_by(Visit.timestamp.desc()).all()
    return render_template('nurse_patient_list.html', visits=visits)

@app.route('/nurse/view_patient/<ic>')
def view_patient_page(ic):
    p = Patient.query.filter_by(ic_number=ic).first_or_404()
    return render_template('nurse_patient_view.html', patient=p)

@app.route('/nurse/get_online_doctors')
def get_online_doctors():
    doctors = User.query.filter_by(role='doctor', status='online').all()
    return jsonify([{'id': d.id, 'name': d.name} for d in doctors])

@app.route('/nurse/register_patient', methods=['POST'])
def register_patient():
    data = request.json
    if Patient.query.filter_by(ic_number=data['ic']).first():
        return jsonify({'error': 'Patient already exists'}), 400
    new_p = Patient(ic_number=data['ic'], name=data['name'], age=data['age'])
    db.session.add(new_p)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/nurse/create_visit', methods=['POST'])
def create_visit():
    data = request.json
    p = Patient.query.filter_by(ic_number=data['ic']).first()
    if not p: return jsonify({'error': 'Patient not found'}), 404
    
    # Doctor Assignment Logic
    requested_doc_id = data.get('doctor_id')
    assigned_id = None

    if requested_doc_id and requested_doc_id != 'auto':
        doc = User.query.get(int(requested_doc_id))
        if doc and doc.status == 'online':
            assigned_id = doc.id
        else:
            return jsonify({'error': 'Selected doctor is currently Away.'}), 400
    else:
        # Random Logic
        online_docs = User.query.filter_by(role='doctor', status='online').all()
        if not online_docs:
            return jsonify({'error': 'No doctors are currently Online.'}), 400
        assigned_id = random.choice(online_docs).id

    visit = Visit(patient_id=p.id, symptoms=data['symptoms'], status='waiting', doctor_id=assigned_id)
    db.session.add(visit)
    db.session.commit()
    
    assigned_name = User.query.get(assigned_id).name
    return jsonify({'success': True, 'assigned_to': assigned_name})

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

@app.route('/nurse/cancel_visit/<int:visit_id>', methods=['POST'])
def cancel_visit(visit_id):
    visit = Visit.query.get_or_404(visit_id)
    visit.status = 'cancelled'
    db.session.commit()
    return jsonify({'success': True})

# --- DOCTOR ROUTES ---
@app.route('/doctor/dashboard')
def doctor_dashboard():
    if session.get('role') != 'doctor': return redirect('/login')
    doctor = User.query.get(session['user_id'])
    queue = Visit.query.filter_by(status='waiting').order_by(Visit.timestamp.asc()).all()
    return render_template('doctor_patients.html', patients=queue, doctor_name=doctor.name, doctor_status=doctor.status, current_user_id=doctor.id)

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
    if visit.doctor_id != session['user_id']:
        return redirect('/doctor/dashboard')
    if visit.status == 'waiting':
        visit.status = 'in_consultation'
        db.session.commit()
    return render_template('consultation.html', visit=visit, patient=visit.patient, doctor_name=session['name'])

@app.route('/save_consultation', methods=['POST'])
def save_consultation():
    data = request.json
    visit = Visit.query.get(data.get('visit_id'))
    if not visit: return jsonify({'error': 'Visit not found'}), 404
    visit.soap_note = data.get('note')
    if data.get('action') == 'finalize': visit.status = 'completed'
    db.session.commit()
    return jsonify({'status': 'success'})

@app.route('/patient/history/<ic>')
def get_patient_history(ic):
    p = Patient.query.filter_by(ic_number=ic).first()
    if not p: return jsonify([])
    history = [{'date': v.timestamp.strftime('%Y-%m-%d %H:%M'), 'symptoms': v.symptoms, 'status': v.status, 'note': v.soap_note or "No notes"} for v in p.visits]
    history.reverse()
    return jsonify(history)

@app.route('/process_audio', methods=['POST'])
def process_audio():
    # Audio processing logic (Mock or Real)
    return jsonify({'transcription': "Mock Text", 'soap_note': "Mock Note"})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if not User.query.first():
            print("Creating Test Users...")
            db.session.add(User(name="Nurse", email='nurse@test.com', password_hash=generate_password_hash('nurse123'), role='nurse'))
            db.session.add(User(name="Dr. Bambi", email='bambi@test.com', password_hash=generate_password_hash('doctor123'), role='doctor', status='online'))
            db.session.add(User(name="Dr. Bambi2", email='bambi2@test.com', password_hash=generate_password_hash('doctor123'), role='doctor', status='away'))
            db.session.add(User(name="Dr. Bambi3", email='bambi3@test.com', password_hash=generate_password_hash('doctor123'), role='doctor', status='online'))
            db.session.add(User(name="Dr. Bambi4", email='bambi4@test.com', password_hash=generate_password_hash('doctor123'), role='doctor', status='online'))
            db.session.commit()
    app.run(debug=True)