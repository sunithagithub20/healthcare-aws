import boto3
from boto3.dynamodb.conditions import Key, Attr
from datetime import datetime
import uuid
import os

# AWS Configuration
# No Access Keys used - uses IAM Role from EC2
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

# Table Names
USERS_TABLE = 'HealthcareUsers'
MEDS_TABLE = 'HealthcareMedications'
LOGS_TABLE = 'HealthcareDoseLogs'
ALERTS_TABLE = 'HealthcareAlertHistory'
VITALS_TABLE = 'HealthcareVitals'

def get_table(name):
    return dynamodb.Table(name)

# --- AUTH & USERS ---
def create_user(user_data):
    table = get_table(USERS_TABLE)
    if 'email' not in user_data:
        return False, "Email is required."
    
    # GetItem (No index needed)
    existing = table.get_item(Key={'email': user_data['email']})
    if 'Item' in existing:
        return False, "User already exists."
    
    try:
        table.put_item(Item=user_data)
        return True, "User created successfully."
    except Exception as e:
        return False, str(e)

def get_user_by_email(email):
    table = get_table(USERS_TABLE)
    response = table.get_item(Key={'email': email})
    return response.get('Item')

def get_user(username):
    # Scan with Filter (No index needed)
    table = get_table(USERS_TABLE)
    response = table.scan(FilterExpression=Attr('username').eq(username))
    items = response.get('Items', [])
    return items[0] if items else None

def get_caregivers():
    table = get_table(USERS_TABLE)
    response = table.scan(FilterExpression=Attr('role').eq('caregiver'))
    return response.get('Items', [])

def get_patients_for_caregiver(caregiver_username):
    table = get_table(USERS_TABLE)
    response = table.scan(FilterExpression=Attr('role').eq('patient') & Attr('assigned_caregiver').eq(caregiver_username))
    return response.get('Items', [])

# --- MEDICATIONS ---
def add_medication(med_data):
    table = get_table(MEDS_TABLE)
    med_data['med_id'] = str(uuid.uuid4()) # Unique PK
    try:
        table.put_item(Item=med_data)
        return True
    except:
        return False

def get_patient_medications(patient_username):
    table = get_table(MEDS_TABLE)
    # Scan with Filter to avoid GSI
    response = table.scan(FilterExpression=Attr('patient_username').eq(patient_username))
    return response.get('Items', [])

# --- DOSE LOGS ---
def log_dose(patient_username, medication_name, scheduled_time, status):
    table = get_table(LOGS_TABLE)
    log_data = {
        "log_id": str(uuid.uuid4()), # Unique PK
        "patient_username": patient_username,
        "medication_name": medication_name,
        "scheduled_time": scheduled_time,
        "status": status,
        "timestamp": datetime.now().isoformat()
    }
    try:
        table.put_item(Item=log_data)
        if status == "Missed":
            user = get_user(patient_username)
            contact = user.get('caregiver_contact', 'N/A')
            log_alert(patient_username, contact, f"Patient explicitly marked {medication_name} as Missed")
        return True
    except:
        return False

def get_patient_dose_logs(patient_username):
    table = get_table(LOGS_TABLE)
    # Scan with Filter to avoid GSI
    response = table.scan(FilterExpression=Attr('patient_username').eq(patient_username))
    return sorted(response.get('Items', []), key=lambda x: x['timestamp'], reverse=True)

def get_all_dose_logs(patient_usernames=None):
    table = get_table(LOGS_TABLE)
    response = table.scan()
    items = response.get('Items', [])
    if patient_usernames:
        items = [i for i in items if i['patient_username'] in patient_usernames]
    return sorted(items, key=lambda x: x['timestamp'], reverse=True)

# --- ALERT HISTORY ---
def log_alert(patient_username, caregiver_contact, alert_msg):
    table = get_table(ALERTS_TABLE)
    alert_data = {
        "alert_id": str(uuid.uuid4()), # Unique PK
        "patient_username": patient_username,
        "caregiver_contact": caregiver_contact,
        "message": alert_msg,
        "timestamp": datetime.now().isoformat(),
        "status": "Sent"
    }
    try:
        table.put_item(Item=alert_data)
    except:
        pass

def get_alert_history(patient_usernames=None):
    table = get_table(ALERTS_TABLE)
    response = table.scan()
    items = response.get('Items', [])
    if patient_usernames:
        items = [i for i in items if i['patient_username'] in patient_usernames]
    return sorted(items, key=lambda x: x['timestamp'], reverse=True)

# --- MONITORING ---
def check_missed_doses():
    """Background job to check for missing medication doses today."""
    patients = get_table(USERS_TABLE).scan(FilterExpression=Attr('role').eq('patient')).get('Items', [])
    today_start = datetime.now().strftime('%Y-%m-%d')
    missed_alerts = []
    
    for patient in patients:
        meds = get_patient_medications(patient['username'])
        for med in meds:
            # Scan logs for today's activity
            logs = get_table(LOGS_TABLE).scan(
                FilterExpression=Attr('patient_username').eq(patient['username']) & 
                                 Attr('medication_name').eq(med['drug_name']) &
                                 Attr('timestamp').begins_with(today_start) &
                                 Attr('status').is_in(['Taken', 'Missed', 'Missed Alerts Sent Today'])
            ).get('Items', [])
            
            if not logs:
                # Dynamically fetch the latest caregiver contact info
                caregiver_username = patient.get('assigned_caregiver')
                caregiver_contact = patient.get('caregiver_contact', 'N/A')
                
                if caregiver_username:
                    caregiver = get_user(caregiver_username)
                    if caregiver:
                        caregiver_contact = caregiver.get('phone_number', caregiver_contact)

                missed_alerts.append({
                    "patient": patient['username'],
                    "medication_name": med['drug_name'],
                    "caregiver_contact": caregiver_contact,
                    "caregiver_username": caregiver_username
                })
                log_dose(patient['username'], med['drug_name'], "Auto-Check", "Missed Alerts Sent Today")
                
    return missed_alerts

# --- VITALS ---
def log_vitals(patient_username, vitals_data):
    table = get_table(VITALS_TABLE)
    vitals_data['vital_id'] = str(uuid.uuid4())
    vitals_data['patient_username'] = patient_username
    vitals_data['timestamp'] = datetime.now().isoformat()
    try:
        table.put_item(Item=vitals_data)
        return True
    except:
        return False

def get_patient_vitals(patient_username):
    table = get_table(VITALS_TABLE)
    response = table.scan(FilterExpression=Attr('patient_username').eq(patient_username))
    return sorted(response.get('Items', []), key=lambda x: x['timestamp'], reverse=True)

def get_latest_vitals_all_patients():
    items = get_table(VITALS_TABLE).scan().get('Items', [])
    latest = {}
    for i in items:
        p = i['patient_username']
        if p not in latest or i['timestamp'] > latest[p]['timestamp']:
            latest[p] = i
    return latest
