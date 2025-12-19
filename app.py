from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta, datetime
import os

app = Flask(__name__)
app.secret_key = os.urandom(24)

# FIX 1: Point to the correct database location in the 'instance' folder
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///instance/medical_scribe.db'
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
    name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.String(10))
    context = db.Column(db.Text)
    status = db.Column(db.String(20), default='waiting')
    # FIX 2: Added timestamp field so the dashboard doesn't crash
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    consultations = db.relationship('Consultation', backref='patient', lazy=True)

class Consultation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patient.id'), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    audio_filename = db.Column(db.String(200))
    transcription_text = db.Column(db.Text)
    soap_note = db.Column(db.Text)
    status = db.Column(db.String(20), default='draft')

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

@app.route('/nurse/dashboard', methods=['GET', 'POST'])
def nurse_dashboard():
    if session.get('role') != 'nurse': return redirect('/login')
    if request.method == 'POST':
        # timestamp is automatically added by the default=datetime.utcnow in the Model
        db.session.add(Patient(name=request.form['name'], age=request.form['age'], context=request.form['context']))
        db.session.commit()
        return redirect('/nurse/dashboard')
    return render_template('nurse_dashboard.html', patients=Patient.query.filter_by(status='waiting').all())

@app.route('/doctor/dashboard')
def doctor_dashboard():
    if session.get('role') != 'doctor': return redirect('/login')
    return render_template('doctor_patients.html', patients=Patient.query.filter_by(status='waiting').all(), doctor_name=session['name'])

@app.route('/start_consultation/<int:patient_id>')
def start_consultation(patient_id):
    if session.get('role') != 'doctor': return redirect('/login')
    patient = Patient.query.get_or_404(patient_id)
    if patient.status == 'waiting':
        patient.status = 'in_consultation'
        db.session.commit()
    return render_template('consultation.html', patient=patient, doctor_name=session['name'])

@app.route('/save_consultation', methods=['POST'])
def save_consultation():
    data = request.json
    consultation = Consultation(
        patient_id=data.get('patient_id'),
        doctor_id=session['user_id'],
        soap_note=data.get('note'),
        status='finalized' if data.get('action') == 'finalize' else 'draft'
    )
    db.session.add(consultation)
    if data.get('action') == 'finalize':
        p = Patient.query.get(data.get('patient_id'))
        if p:
            p.status = 'completed'
    db.session.commit()
    return jsonify({'status': 'success'})

@app.route('/create-demo-users')
def create_demo_users():
    if not User.query.first():
        # Using 'pbkdf2:sha256' is default safe method if scrypt is unavailable, 
        # but sticking to your method is fine. Ensure strict matching if specific hashing is required.
        db.session.add(User(name="Nurse Joy", email='nurse@test.com', password_hash=generate_password_hash('nurse123'), role='nurse'))
        db.session.add(User(name="Dr. Strange", email='doctor@test.com', password_hash=generate_password_hash('doctor123'), role='doctor'))
        db.session.commit()
    return "Demo users created"

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)