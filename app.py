import os
from datetime import datetime
from functools import wraps
import traceback

import firebase_admin
from flask import Flask, request, jsonify
from firebase_admin import credentials, db
from twilio.twiml.messaging_response import MessagingResponse

from openai import OpenAI
import requests

# Initialize Flask
app = Flask(__name__)

# Firebase setup
cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://soft-whatsapp-chatbot-default-rtdb.firebaseio.com/'
})

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")

# WhatsApp Cloud API send message helper
# Added debug print to log WhatsApp API response for troubleshooting
def send_whatsapp_message(to, message):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    response = requests.post(url, headers=headers, json=data)
    print(f"[DEBUG] WhatsApp API response: {response.status_code} {response.text}")
    return response.json()

# Helper: Validate Twilio signature (stub, implement for production)
def validate_twilio(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # TODO: Implement signature validation for production
        return f(*args, **kwargs)
    return decorated

# Helper: Get student's department
def get_department(whatsapp):
    try:
        ref = db.reference(f'students/{whatsapp}')
        data = ref.get()
        if data and isinstance(data, dict) and 'department' in data:
            return data['department']
    except Exception as e:
        print(f"[DEBUG] get_department error for {whatsapp}: {e}")
    return 'SE'

# Helper: Find next class for department
def get_next_class(department):
    try:
        ref = db.reference(f'classes/{department}')
        classes = ref.get() or {}
        today = datetime.now().date()
        next_class = None
        for code, details in classes.items():
            try:
                if not isinstance(details, dict):
                    continue
                class_date = datetime.strptime(details.get('date', ''), '%Y-%m-%d').date()
                if class_date >= today:
                    if not next_class or class_date < datetime.strptime(next_class['date'], '%Y-%m-%d').date():
                        next_class = {**details, 'course_code': code}
            except Exception as e:
                print(f"[DEBUG] get_next_class error for {code}: {e}")
                continue
        return next_class
    except Exception as e:
        print(f"[DEBUG] get_next_class outer error: {e}")
    return None

# ----------------------
# Main WhatsApp webhook endpoint (GET for verification, POST for messages)
# ----------------------
@app.route('/webhook', methods=['GET', 'POST'])
def whatsapp_webhook():
    # Webhook verification for Meta/WhatsApp setup
    if request.method == 'GET':
        verify_token = "mysecrettoken"  # Use the same token you set in Meta
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == verify_token:
            return challenge, 200
        else:
            return "Verification failed", 403
    # --- BEGIN: 415 error handling ---
    # Ensure incoming POST is JSON (required by WhatsApp Cloud API)
    if not request.is_json:
        print("[DEBUG] /webhook received non-JSON POST. Headers:", dict(request.headers))
        return jsonify({'error': 'Content-Type must be application/json'}), 415
    # --- END: 415 error handling ---
    data = request.get_json()
    try:
        # Parse WhatsApp Cloud API message structure
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        messages = value.get("messages")
        if messages:
            msg = messages[0]
            from_number = msg["from"]  # WhatsApp ID (phone number)
            print("Received message from:", from_number)
            body = msg["text"]["body"].strip()
            # Admin check: see if sender is an admin in Firebase
            admin_ref = db.reference(f'admins/{from_number}')
            is_admin = admin_ref.get() is True
            # Admin can update class schedule via special command
            if body.lower().startswith('adminupdate') and is_admin:
                try:
                    _, dept, code, date, time, *lecturer = body.split()
                    lecturer = ' '.join(lecturer)
                    class_ref = db.reference(f'classes/{dept}/{code}')
                    class_ref.set({
                        'date': date,
                        'time': time,
                        'lecturer': lecturer
                    })
                    send_whatsapp_message(from_number, f'Schedule updated for {code}.')
                except Exception:
                    send_whatsapp_message(from_number, 'Error: Invalid adminupdate format.')
                return "OK", 200
            # Student asks for next class
            elif body.lower() in ['next class', 'when is my next class?']:
                dept = get_department(from_number)
                # Auto-register student if not present
                if dept == 'SE':
                    ref = db.reference(f'students/{from_number}')
                    ref.set({'department': 'SE'})
                next_class = get_next_class('SE')
                if next_class:
                    send_whatsapp_message(from_number, f"Your next class is {next_class['course_code']} on {next_class['date']} at {next_class['time']} with {next_class['lecturer']}.")
                else:
                    send_whatsapp_message(from_number, 'No upcoming classes found.')
                return "OK", 200
            # Student asks for their courses
            elif body.lower() == 'my courses':
                dept = get_department(from_number)
                ref = db.reference(f'classes/{dept}')
                classes = ref.get() or {}
                if classes:
                    course_list = list(classes.keys())
                    send_whatsapp_message(from_number, f"Your courses: {', '.join(course_list)}\nReply with a course code to get details.")
                else:
                    send_whatsapp_message(from_number, 'No courses found for your department.')
                return "OK", 200
            else:
                # If message matches a course code, return class details
                dept = get_department(from_number)
                ref = db.reference(f'classes/{dept}')
                classes = ref.get() or {}
                if body in classes:
                    class_details = classes[body]
                    send_whatsapp_message(from_number, f"Course: {body}\nDate: {class_details.get('date', 'N/A')}\nTime: {class_details.get('time', 'N/A')}\nLecturer: {class_details.get('lecturer', 'N/A')}")
                    return "OK", 200
                # Otherwise, use OpenAI for general SE questions
                try:
                    completion = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {"role": "system", "content": "Your name is Zibot and you are a helpful assistant for Software Engineering students at SDU. Answer questions clearly and concisely."},
                            {"role": "user", "content": body}
                        ]
                    )
                    answer = completion.choices[0].message.content.strip()
                    send_whatsapp_message(from_number, answer)
                except Exception:
                    send_whatsapp_message(from_number, 'Sorry, I could not process your request right now.')
                return "OK", 200
            # AI introduction for first message in a chat
            user_ref = db.reference(f'users/{from_number}/introduced')
            introduced = user_ref.get()
            if not introduced:
                intro_message = "Hello! My name is Zibot, your helpful assistant for Software Engineering students at SDU. Ask me about your schedule, classes, or any SE topic!"
                send_whatsapp_message(from_number, intro_message)
                user_ref.set(True)
                return "OK", 200
    except Exception as e:
        print("Webhook error:", e)
        traceback.print_exc()
    return "OK", 200

@app.route('/register-student', methods=['POST'])
def register_student():
    data = request.get_json()
    whatsapp = data.get('whatsapp')
    department = data.get('department')
    if not whatsapp or not department:
        return jsonify({'error': 'Missing whatsapp or department'}), 400
    ref = db.reference(f'students/{whatsapp}')
    ref.set({'department': department})
    return jsonify({'message': 'Student registered.'}), 200

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
