from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta, datetime
import os
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- SYSTEMATIC FIX: Absolute Path & Directory Creation ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_FOLDER = os.path.join(BASE_DIR, 'instance')
if not os.path.exists(INSTANCE_FOLDER):
    os.makedirs(INSTANCE_FOLDER)

db_path = os.path.join(INSTANCE_FOLDER, 'medical_scribe.db')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.permanent_session_lifetime = timedelta(minutes=15)
db = SQLAlchemy(app)

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)

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
    timestamp = db.Column(db.DateTime, default=datetime.now)
    symptoms = db.Column(db.Text, nullable=False)
    soap_note = db.Column(db.Text)
    status = db.Column(db.String(20), default='waiting')

# --- ROUTES ---
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
            return redirect('/')
        flash('Invalid credentials', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# --- NURSE ROUTES ---
@app.route('/nurse/update_patient', methods=['POST'])
def update_patient():
    data = request.json
    p = Patient.query.filter_by(ic_number=data['ic']).first()
    if not p: return jsonify({'error': 'Patient not found'}), 404
    p.name = data['name']
    p.age = data['age']
    db.session.commit()
    return jsonify({'success': True})

@app.route('/nurse/dashboard')
def nurse_dashboard():
    if session.get('role') != 'nurse': return redirect('/login')
    queue = Visit.query.filter(Visit.status.in_(['waiting', 'in_consultation'])).order_by(Visit.timestamp.asc()).all()
    return render_template('nurse_dashboard.html', queue=queue)

@app.route('/nurse/search_patient', methods=['POST'])
def search_patient():
    ic = request.json.get('ic_number')
    patient = Patient.query.filter_by(ic_number=ic).first()
    if patient:
        return jsonify({'found': True, 'id': patient.id, 'name': patient.name, 'age': patient.age})
    return jsonify({'found': False})

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
    visit = Visit(patient_id=p.id, symptoms=data['symptoms'], status='waiting')
    db.session.add(visit)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/nurse/cancel_visit/<int:visit_id>', methods=['POST'])
def cancel_visit(visit_id):
    visit = Visit.query.get_or_404(visit_id)
    visit.status = 'cancelled'
    db.session.commit()
    return jsonify({'success': True})


#  Nurse Patient List
@app.route('/nurse/patient_list')
def nurse_patient_list():
    if session.get('role') != 'nurse': return redirect('/login')
    
    # Fetch all visits, sorted by newest first
    # We join with Patient to ensure we can search/sort by name if needed
    visits = Visit.query.join(Patient).order_by(Visit.timestamp.desc()).all()
    
    return render_template('nurse_patient_list.html', visits=visits)

# NURSE VIEW/DELETE
@app.route('/nurse/view_patient/<ic>')
def view_patient_page(ic):
    if session.get('role') != 'nurse': return redirect('/login')
    p = Patient.query.filter_by(ic_number=ic).first_or_404()
    return render_template('nurse_patient_view.html', patient=p)

@app.route('/nurse/delete_patient', methods=['POST'])
def delete_patient():
    data = request.json
    p = Patient.query.filter_by(ic_number=data['ic']).first()
    
    if not p:
        return jsonify({'error': 'Patient not found'}), 404
    
    # 1. Manually delete all visits first (to ensure clean cascade)
    Visit.query.filter_by(patient_id=p.id).delete()
    
    # 2. Delete the patient
    db.session.delete(p)
    db.session.commit()
    
    return jsonify({'success': True})




# --- DOCTOR ROUTES ---
@app.route('/doctor/dashboard')
def doctor_dashboard():
    if session.get('role') != 'doctor': return redirect('/login')
    queue = Visit.query.filter_by(status='waiting').order_by(Visit.timestamp.asc()).all()
    return render_template('doctor_patients.html', patients=queue, doctor_name=session['name'])

# NEW ROUTE: Dedicated History Page
@app.route('/doctor/history')
def doctor_history_page():
    if session.get('role') != 'doctor': return redirect('/login')
    return render_template('doctor_history.html', doctor_name=session['name'])

@app.route('/start_consultation/<int:visit_id>')
def start_consultation(visit_id):
    if session.get('role') != 'doctor': return redirect('/login')
    visit = Visit.query.get_or_404(visit_id)
    if visit.status == 'waiting':
        visit.status = 'in_consultation'
        visit.doctor_id = session['user_id']
        db.session.commit()
    return render_template('consultation.html', visit=visit, patient=visit.patient, doctor_name=session['name'])

@app.route('/save_consultation', methods=['POST'])
def save_consultation():
    data = request.json
    visit = Visit.query.get(data.get('visit_id'))
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
    history = []
    for v in p.visits:
        history.append({
            'date': v.timestamp.strftime('%Y-%m-%d %H:%M'),
            'symptoms': v.symptoms,
            'status': v.status,
            'note': v.soap_note or "No notes"
        })
    history.reverse()
    return jsonify(history)



# --- AUDIO & AI ---
@app.route('/process_audio', methods=['POST'])
def process_audio():
    if 'audio_data' not in request.files:
        return jsonify({'error': 'No audio file'}), 400
    
    audio_file = request.files['audio_data']
    visit_id = request.form.get('visit_id')
    
    filename = f"visit_{visit_id}.wav"
    save_path = os.path.join(INSTANCE_FOLDER, filename)
    audio_file.save(save_path)

   ##### # Mock AI (Replace with real OpenAI call if needed)
    text = "Patient complains of persistent cough and headache for 3 days."
    soap = f"S: Cough, Headache (3 days)\nO: N/A\nA: Viral URI\nP: Symptomatic relief"
    
    return jsonify({'transcription': text, 'soap_note': soap})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if not User.query.first():
            db.session.add(User(name="Bambee", email='nurse@test.com', password_hash=generate_password_hash('nurse123'), role='nurse'))
            db.session.add(User(name="Bambi", email='doctor@test.com', password_hash=generate_password_hash('doctor123'), role='doctor'))
            db.session.commit()
    app.run(debug=True)