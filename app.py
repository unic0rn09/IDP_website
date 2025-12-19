from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta, datetime
import os

app = Flask(__name__)

# ---------------- CONFIG ----------------
app.secret_key = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///medical_scribe.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Auto logout after 15 minutes
app.permanent_session_lifetime = timedelta(minutes=15)

db = SQLAlchemy(app)

# ---------------- DATABASE MODELS ----------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False) # <--- This was causing the error!
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)

class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.String(10))
    context = db.Column(db.Text)
    status = db.Column(db.String(20), default='waiting')
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# ---------------- ROUTES ----------------
@app.route('/')
def index():
    if 'user_id' in session:
        if session['role'] == 'doctor':
            return redirect('/doctor/dashboard')
        else:
            return redirect('/nurse/dashboard')
    return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            session.permanent = True
            session['user_id'] = user.id
            session['role'] = user.role
            session['name'] = user.name
            return redirect('/')
        else:
            flash('Invalid email or password', 'error')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# ---------------- NURSE DASHBOARD ----------------
@app.route('/nurse/dashboard', methods=['GET', 'POST'])
def nurse_dashboard():
    if session.get('role') != 'nurse':
        return redirect('/login')

    # Handle Patient Registration
    if request.method == 'POST':
        name = request.form['name']
        age = request.form['age']
        context = request.form['context']
        
        new_patient = Patient(name=name, age=age, context=context, status='waiting')
        db.session.add(new_patient)
        db.session.commit()
        return redirect('/nurse/dashboard')

    # Show Waiting List
    waiting_patients = Patient.query.filter_by(status='waiting').all()
    return render_template('nurse_dashboard.html', patients=waiting_patients)

# ---------------- DOCTOR DASHBOARD ----------------
@app.route('/doctor/dashboard')
def doctor_dashboard():
    if session.get('role') != 'doctor':
        return redirect('/login')
    
    waiting_patients = Patient.query.filter_by(status='waiting').all()
    return render_template('doctor_patients.html', patients=waiting_patients, doctor_name=session['name'])

# Make sure you have this import at the top of app.py!
from datetime import datetime 

@app.route('/start_consultation/<int:patient_id>')
def start_consultation(patient_id):
    if session.get('role') != 'doctor':
        return redirect('/login')
        
    # 1. Fetch the specific patient by ID
    patient = Patient.query.get_or_404(patient_id)
    
    # 2. Update status to 'in_consultation' so other doctors know they are busy
    patient.status = 'in_consultation'
    db.session.commit()
    
    # 3. Get current time for the display
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 4. Render the new Consultation Page with all data
    return render_template('consultation.html', 
                           patient=patient, 
                           doctor_name=session['name'],
                           current_time=current_time)

# ---------------- INIT (THE BUG FIX) ----------------
@app.route('/create-demo-users')
def create_demo_users():
    if User.query.first():
        return "Users already exist"

    # I ADDED 'name' HERE. This was missing in your file!
    nurse = User(
        name="Nurse Joy", 
        email='nurse@test.com',
        password_hash=generate_password_hash('nurse123'),
        role='nurse'
    )

    doctor = User(
        name="Dr. Strange",
        email='doctor@test.com',
        password_hash=generate_password_hash('doctor123'),
        role='doctor'
    )

    db.session.add(nurse)
    db.session.add(doctor)
    db.session.commit()

    return "Demo users created successfully"

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)