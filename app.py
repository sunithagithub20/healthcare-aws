import boto3
import logging
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from datetime import datetime
import db_handler
import threading
import time
import os

# Initialize Flask app
app = Flask(__name__)
app.secret_key = 'healthcare_secret_key'

# --- AWS SERVICES (SNS, SSM, CloudWatch) ---
# CloudWatch Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Boto3 Clients (Will use EC2 IAM Role automatically)
sns = boto3.client('sns', region_name='us-east-1')
ssm = boto3.client('ssm', region_name='us-east-1')

def get_sns_topic():
    try:
        # Get SNS Topic ARN from SSM Parameter Store (Dynamic Config)
        parameter = ssm.get_parameter(Name='/Healthcare/SNS_TOPIC_ARN', WithDecryption=False)
        return parameter['Parameter']['Value'].strip()
    except Exception as e:
        logger.error(f"Error fetching SNS ARN from SSM: {e}")
        return None

def trigger_sns_alert(patient_username, caregiver_contact, alert_msg):
    topic_arn = get_sns_topic()
    
    # 1. Log to CloudWatch
    logger.info(f"ALERT SYSTEM: {alert_msg}")
    
    # 2. Publish to SNS (Service 2)
    if topic_arn:
        try:
            sns.publish(
                TopicArn=topic_arn,
                Message=alert_msg,
                Subject=f"VitalGuard Alert: {patient_username}"
            )
            logger.info(f"SNS Topic alert sent for {patient_username}.")
        except Exception as e:
            logger.error(f"SNS Publish Error: {e}")
    else:
        logger.warning(f"SNS Topic ARN missing in SSM. Logged alert for {patient_username}: {alert_msg}")

# --- BACKGROUND MONITORING ---
def background_checker():
    logger.info("Background Adherence Checker Started.")
    while True:
        try:
            missed = db_handler.check_missed_doses()
            for m in missed:
                alert_msg = f"[VITALGUARD ALERT] Patient {m['patient']} missed their dose of {m['medication_name']}! Could you please check on them?"
                trigger_sns_alert(m['patient'], m['caregiver_contact'], alert_msg)
                
                # Log the alert event in history
                db_handler.log_alert(m['patient'], m['caregiver_contact'], alert_msg)
        except Exception as e:
            logger.error(f"Error in background checker: {e}")
        time.sleep(60)

# --- ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Login using email as the Partition Key for DynamoDB
        email = request.form['email']
        password = request.form['password']
        
        user = db_handler.get_user_by_email(email)
        if user and user['password'] == password:
            session['username'] = user['username']
            session['email'] = user['email']
            session['role'] = user['role']
            session['name'] = user['name']
            
            if user['role'] == 'patient':
                return redirect(url_for('patient_dashboard'))
            else:
                return redirect(url_for('caregiver_dashboard'))
        else:
            return render_template('login.html', error="Invalid Email or Password")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        user_data = dict(request.form)
        
        # Automatically fetch caregiver contact for patients
        if user_data.get('role') == 'patient' and user_data.get('assigned_caregiver'):
            # Search for the caregiver by username to get their details
            caregiver = db_handler.get_user(user_data['assigned_caregiver'])
            if caregiver:
                user_data['caregiver_contact'] = caregiver.get('phone_number', 'N/A')
            else:
                user_data['caregiver_contact'] = 'N/A'
        
        success, message = db_handler.create_user(user_data)
        if success:
            return redirect(url_for('login'))
        else:
            caregivers = db_handler.get_caregivers()
            return render_template('register.html', error=message, caregivers=caregivers)
    
    caregivers = db_handler.get_caregivers()
    return render_template('register.html', caregivers=caregivers)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# --- PATIENT DASHBOARD ---
@app.route('/patient_dashboard')
def patient_dashboard():
    if 'username' not in session or session['role'] != 'patient':
        return redirect(url_for('login'))
    
    meds = db_handler.get_patient_medications(session['username'])
    vitals = db_handler.get_patient_vitals(session['username'])
    
    # Get user details for profile section
    user_details = db_handler.get_user_by_email(session['email'])
    
    # Fetch caregiver email dynamically
    caregiver_email = "N/A"
    if user_details and user_details.get('assigned_caregiver'):
        caregiver = db_handler.get_user(user_details['assigned_caregiver'])
        if caregiver:
            caregiver_email = caregiver.get('email', 'N/A')
    
    return render_template('patient_dashboard.html', 
                           patient=user_details,
                           caregiver_email=caregiver_email,
                           meds=meds, 
                           vitals=vitals)

@app.route('/api/log_specific_dose', methods=['POST'])
def log_specific_dose():
    data = request.json
    med_name = data.get('medication_name')
    status = data.get('status')
    
    if db_handler.log_dose(session['username'], med_name, "Manual", status):
        # Trigger real-time SNS alert if manually marked as missed
        if status == "Missed":
            user = db_handler.get_user_by_email(session['email'])
            contact = user.get('caregiver_contact', 'N/A')
            
            alert_msg = f"[VITALGUARD ALERT] Patient {session['username']} marked medication {med_name} as MISSED! Could you please check on them?"
            trigger_sns_alert(session['username'], contact, alert_msg)
        return jsonify({"success": True})
    return jsonify({"success": False})

@app.route('/patient_vitals')
def patient_vitals():
    if 'username' not in session or session['role'] != 'patient':
        return redirect(url_for('login'))
        
    return render_template('patient_vitals.html')

@app.route('/api/log_vitals', methods=['POST'])
def api_log_vitals():
    vitals_data = request.json
    username = session.get('username')
    email = session.get('email')
    
    # Range Checking Logic
    status = "Stable"
    alert_reasons = []
    
    try:
        hr = int(vitals_data.get('hr', 75))
        glucose = int(vitals_data.get('glucose', 100))
        bp = vitals_data.get('bp', '120/80')
        
        if hr < 50 or hr > 110:
            status = "Warning"
            alert_reasons.append(f"Abnormal Heart Rate: {hr} BPM")
        
        if glucose < 60 or glucose > 200:
            status = "Warning"
            alert_reasons.append(f"Abnormal Glucose: {glucose} mg/dL")
            
        if '/' in bp:
            systolic, diastolic = map(int, bp.split('/'))
            if systolic > 150 or systolic < 90 or diastolic > 95 or diastolic < 60:
                status = "Warning"
                alert_reasons.append(f"Abnormal Blood Pressure: {bp}")
    except:
        pass # Ignore parsing errors for now
        
    vitals_data['status'] = status
    
    if db_handler.log_vitals(username, vitals_data):
        if status == "Warning":
            user = db_handler.get_user_by_email(email)
            contact = user.get('caregiver_contact', 'N/A')

            alert_msg = f"[VITALGUARD VITAL ALERT] Patient {username} has abnormal health readings: {', '.join(alert_reasons)}. Could you please check on them?"
            
            # Use SNS trigger
            trigger_sns_alert(username, contact, alert_msg)
            db_handler.log_alert(username, contact, alert_msg)
            
        return jsonify({"success": True})
    return jsonify({"success": False})

# --- CAREGIVER DASHBOARD ---
@app.route('/caregiver_dashboard')
def caregiver_dashboard():
    if 'username' not in session or session['role'] != 'caregiver':
        return redirect(url_for('login'))
        
    patients = db_handler.get_patients_for_caregiver(session['username'])
    patient_usernames = [p['username'] for p in patients]
    
    alert_history = db_handler.get_alert_history(patient_usernames)
    patient_vitals = db_handler.get_latest_vitals_all_patients()
    
    return render_template('caregiver_dashboard.html', 
                           username=session['username'], 
                           patients=patients,
                           alert_history=alert_history,
                           patient_vitals=patient_vitals)

@app.route('/assign_meds', methods=['GET', 'POST'])
def assign_meds():
    if 'username' not in session or session['role'] != 'caregiver':
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        med_data = {
            'patient_username': request.form['patient_username'],
            'drug_name': request.form['drug_name'],
            'dosage': request.form['dosage'],
            'timing': request.form['timing'],
            'start_date': request.form['start_date'],
            'assigned_by': session['username'],
            'created_at': datetime.now().isoformat()
        }
        db_handler.add_medication(med_data)
        return redirect(url_for('caregiver_dashboard'))
        
    patients = db_handler.get_patients_for_caregiver(session['username'])
    return render_template('assign_meds.html', patients=patients)

if __name__ == '__main__':
    # Start background thread
    threading.Thread(target=background_checker, daemon=True).start()
    
    # Run Flask on Port 5000 (EC2 Default)
    app.run(debug=False, host='0.0.0.0', port=5000)
