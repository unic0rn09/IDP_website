# Phase 1: Flask App Foundation with Secure Authentication
# ================================================
# This file shows the CORE working app (app.py) for Phase 1
# Features:
# - Email & password login
# - Password hashing
# - Role-based users (Doctor / Nurse)
# - Session timeout for patient privacy

from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta
import os

app = Flask(__name__)

# ---------------- CONFIG ----------------
app.secret_key = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///medical_scribe.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Auto logout after 15 minutes inactivity
app.permanent_session_lifetime = timedelta(minutes=15)

db = SQLAlchemy(app)

# ---------------- DATABASE MODEL ----------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # doctor or nurse

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

# ---------------- DASHBOARDS ----------------
@app.route('/doctor/dashboard')
def doctor_dashboard():
    if session.get('role') != 'doctor':
        return redirect('/login')
    return f"Welcome Dr. {session['name']}"

@app.route('/nurse/dashboard')
def nurse_dashboard():
    if session.get('role') != 'nurse':
        return redirect('/login')
    return f"Welcome Nurse {session['name']}"

# ---------------- INIT ----------------
#USER INFO FOR TESTING PURPOSES (IN LOGIN PAGE)
from werkzeug.security import generate_password_hash

@app.route('/create-demo-users')
def create_demo_users():
    if User.query.first():
        return "Users already exist"

    nurse = User(
        email='nurse@test.com',
        password_hash=generate_password_hash('nurse123'),
        role='nurse'
    )

    doctor = User(
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
#abcdefg