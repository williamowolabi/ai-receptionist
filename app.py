from flask import Flask, request, send_file, Response
from twilio.twiml.voice_response import VoiceResponse, Gather, Dial
from twilio.rest import Client
from openai import OpenAI
import os
import csv
import hashlib
from datetime import datetime
from urllib.parse import urlencode

app = Flask(__name__)

DATA_FILE = "/var/data/calls.csv" if os.path.exists("/var/data") else "calls.csv"
LANGUAGE = "en-US"

# --- OPENAI ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
OPENAI_VOICE = "nova"

# --- TWILIO CREDENTIALS ---
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")

# --- YOUR CALENDLY LINK ---
CALENDLY_LINK = os.environ.get("CALENDLY_LINK", "https://calendly.com/your-link-here")

# --- YOUR PUBLIC APP URL ---
APP_URL = os.environ.get("APP_URL", "").rstrip("/")

# --- BUSINESS OWNER PHONE ---
# The number that receives lead alerts and urgent call forwards
OWNER_PHONE = os.environ.get("OWNER_PHONE", "")

# --- MAX RETRIES PER STEP ---
# After this many failed attempts the call gracefully ends
MAX_RETRIES = 3

# In-memory cache
text_cache = {}
audio_cache = {}

# Hints help Twilio recognize expected words and ignore noise
GENERAL_HINTS = (
    "yes, no, yeah, nope, plumbing, HVAC, electrical, roofing, "
    "landscaping, painting, flooring, urgent, not urgent, "
    "appointment, schedule, help, repair, install, replace, fix, "
    "leak, broken, damage, emergency, inspection, agent, operator, human"
)


def generate_speech(text):
    """Generate speech using OpenAI TTS and return mp3 bytes."""
    if not openai_client:
        return None
    try:
        resp = openai_client.audio.speech.create(
            model="tts-1",
            voice=OPENAI_VOICE,
            input=text,
            response_format="mp3"
        )
        return resp.content
    except Exception as e:
        print(f"OpenAI TTS error: {e}")
        return None


def say(response, text):
    """Play OpenAI TTS audio or fall back to Polly."""
    if openai_client and APP_URL:
        key = hashlib.md5(text.encode()).hexdigest()
        text_cache[key] = text
        audio_url = f"{APP_URL}/audio/{key}"
        response.play(audio_url)
    else:
        response.say(text, voice="Polly.Joanna", language=LANGUAGE)


@app.route("/audio/<key>", methods=["GET"])
def serve_audio(key):
    """Generate and serve OpenAI TTS audio on demand."""
    text = text_cache.get(key)
    if not text:
        return "Audio not found", 404
    if key in audio_cache:
        return Response(audio_cache[key], mimetype="audio/mpeg")
    audio_data = generate_speech(text)
    if not audio_data:
        return "Audio generation failed", 500
    audio_cache[key] = audio_data
    return Response(audio_data, mimetype="audio/mpeg")


def gather_speech(action_url, hints=GENERAL_HINTS):
    """Gather speech and keypad input with noise filtering."""
    return Gather(
        input="speech dtmf",
        action=action_url,
        method="POST",
        speech_timeout="auto",
        language=LANGUAGE,
        enhanced=True,
        speech_model="phone_call",
        hints=hints,
        profanity_filter=False,
        num_digits=1
    )


def build_url(path, **params):
    if not params:
        return path
    return f"{path}?{urlencode(params)}"


def clean_text(value):
    """Clean and validate speech input."""
    if not value:
        return ""
    value = value.strip()
    if len(value) < 3:
        return ""
    if all(c in ".,!?- " for c in value):
        return ""
    return value


def extract_name(value):
    """Strip common name prefixes so we get just the name."""
    if not value:
        return ""
    text = value.strip()
    prefixes = [
        "my name is ", "my name's ", "this is ", "it's ", "its ",
        "i'm ", "im ", "i am ", "the name is ", "name is ", "call me ",
        "hi my name is ", "hello my name is ", "hey my name is ",
        "hi i'm ", "hello i'm ", "hi this is ", "hello this is ",
    ]
    lower = text.lower()
    for prefix in prefixes:
        if lower.startswith(prefix):
            text = text[len(prefix):]
            break
    if len(text.strip()) < 2:
        return ""
    return text.strip().title()


def yes_no_answer(text):
    t = clean_text(text).lower()
    if any(w in t for w in ["yes", "yeah", "yep", "correct", "right", "affirmative", "sure", "absolutely"]):
        return "yes"
    if any(w in t for w in ["no", "nope", "nah", "incorrect", "wrong", "negative"]):
        return "no"
    return ""


def wants_agent(speech, digit):
    """Check if caller pressed 0 or asked for a human."""
    if digit == "0":
        return True
    if speech:
        t = speech.lower()
        if any(w in t for w in ["agent", "operator", "human", "person", "representative", "help"]):
            return True
    return False


def transfer_to_agent(response, name=""):
    """Transfer caller to the business owner or play a message if no number set."""
    if OWNER_PHONE:
        say(response, f"Please hold while I connect you with a team member.")
        dial = Dial(action="/call_ended", method="POST")
        dial.number(OWNER_PHONE)
        response.append(dial)
    else:
        say(response, (
            "I'm sorry, all of our team members are currently unavailable. "
            "Please leave a message after the tone or call back during business hours."
        ))
        response.record(max_length=60, action="/voicemail_saved")
    return str(response)


def send_sms(to_number, body):
    """Core SMS sending function."""
    try:
        if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
            print("Twilio credentials missing — skipping SMS")
            return False
        if not to_number or to_number == "Unknown":
            print("No valid phone number — skipping SMS")
            return False
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=body, from_=TWILIO_PHONE_NUMBER, to=to_number)
        print(f"SMS sent to {to_number}")
        return True
    except Exception as e:
        print(f"SMS failed to {to_number}: {e}")
        return False


def send_booking_sms(to_number, name, service):
    """Send Calendly booking link to the customer after the call."""
    body = (
        f"Hi {name}, thanks for calling!\n\n"
        f"To book your {service} appointment click below:\n"
        f"{CALENDLY_LINK}\n\n"
        f"We look forward to helping you!"
    )
    send_sms(to_number, body)


def send_lead_alert(name, caller, service, urgency, intent):
    """Alert the business owner a new lead came in."""
    if not OWNER_PHONE:
        print("OWNER_PHONE not set — skipping lead alert")
        return
    urgency_flag = "🚨 URGENT" if urgency == "Urgent" else "📋 Standard"
    body = (
        f"📞 New Lead — NextReceptionist\n\n"
        f"{urgency_flag}\n"
        f"Name: {name}\n"
        f"Service: {service}\n"
        f"Issue: {intent}\n"
        f"Phone: {caller}\n\n"
        f"Booking link sent to customer."
    )
    send_sms(OWNER_PHONE, body)


def send_urgent_alert(name, caller, service, intent):
    """
    URGENT CALL ALERT — Sent immediately when customer marks call as urgent.
    Business owner gets notified right away so they can call back fast.
    """
    if not OWNER_PHONE:
        return
    body = (
        f"🚨 URGENT CALL — NextReceptionist\n\n"
        f"Customer needs immediate help!\n\n"
        f"Name: {name}\n"
        f"Service: {service}\n"
        f"Issue: {intent}\n"
        f"Phone: {caller}\n\n"
        f"Call them back ASAP!"
    )
    send_sms(OWNER_PHONE, body)
    print(f"Urgent alert sent for {name}")


def ensure_csv_exists():
    folder = os.path.dirname(DATA_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "name", "caller_phone", "service", "intent", "urgency", "details"])


def append_to_csv(name, caller, service, intent, urgency, details):
    with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            name, caller, service, intent, urgency, details
        ])
    print("Call saved to CSV")


ensure_csv_exists()


@app.route("/", methods=["GET"])
def home():
    return "AI Receptionist is running."


@app.route("/voice", methods=["GET", "POST"])
def voice():
    response = VoiceResponse()
    gather = gather_speech("/get_name")
    say(gather, (
        "Thank you for calling. You've reached the service desk. "
        "What is your full name please? "
        "At any time you can press zero to speak with someone directly."
    ))
    response.append(gather)
    response.redirect("/voice")
    return str(response)


@app.route("/get_name", methods=["POST"])
def get_name():
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    digit = request.values.get("Digits", "")
    caller = request.values.get("From", "Unknown")
    retries = int(request.args.get("retries", 0))

    # Check if caller wants a human
    if wants_agent(speech, digit):
        return transfer_to_agent(response)

    name = extract_name(speech)

    if not name:
        if retries >= MAX_RETRIES:
            say(response, "I'm having trouble hearing you. Let me connect you with someone who can help.")
            return transfer_to_agent(response)
        retry_url = build_url("/get_name", retries=retries + 1)
        gather = gather_speech(retry_url)
        say(gather, "I didn't catch your name. Could you please say your full name? Or press zero to speak with someone.")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url("/get_service", name=name, caller=caller)
    gather = gather_speech(next_url)
    say(gather, f"Thanks {name}. What type of service do you need today? For example, plumbing, HVAC, electrical, or roofing.")
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_service", methods=["POST"])
def get_service():
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    digit = request.values.get("Digits", "")
    name = request.args.get("name", "")
    caller = request.args.get("caller", "Unknown")
    retries = int(request.args.get("retries", 0))

    if wants_agent(speech, digit):
        return transfer_to_agent(response, name)

    service = clean_text(speech)

    if not service:
        if retries >= MAX_RETRIES:
            say(response, "I'm having trouble hearing you. Let me connect you with someone who can help.")
            return transfer_to_agent(response, name)
        retry_url = build_url("/get_service", name=name, caller=caller, retries=retries + 1)
        gather = gather_speech(retry_url)
        say(gather, "I didn't catch that. Please tell me the type of service you need, like plumbing, HVAC, or electrical. Or press zero to speak with someone.")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    confirm_url = build_url("/confirm_service", name=name, service=service, caller=caller)
    gather = gather_speech(confirm_url)
    say(gather, f"Got it, you need {service}. Is that correct? Please say yes or no.")
    response.append(gather)
    response.redirect(confirm_url)
    return str(response)


@app.route("/confirm_service", methods=["POST"])
def confirm_service():
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    digit = request.values.get("Digits", "")
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    caller = request.args.get("caller", "Unknown")
    retries = int(request.args.get("retries", 0))

    if wants_agent(speech, digit):
        return transfer_to_agent(response, name)

    answer = yes_no_answer(speech)

    if answer == "yes":
        next_url = build_url("/get_intent", name=name, service=service, caller=caller)
        gather = gather_speech(next_url)
        say(gather, "Great. Can you briefly describe what is going on and what you need help with?")
        response.append(gather)
        response.redirect(next_url)
        return str(response)

    if answer == "no":
        next_url = build_url("/get_service", name=name, caller=caller)
        gather = gather_speech(next_url)
        say(gather, "No problem, let's try again. What type of service do you need?")
        response.append(gather)
        response.redirect(next_url)
        return str(response)

    if retries >= MAX_RETRIES:
        say(response, "Let me connect you with someone who can help.")
        return transfer_to_agent(response, name)

    retry_url = build_url("/confirm_service", name=name, service=service, caller=caller, retries=retries + 1)
    gather = gather_speech(retry_url)
    say(gather, "Sorry, I didn't catch that. Please say yes or no.")
    response.append(gather)
    response.redirect(retry_url)
    return str(response)


@app.route("/get_intent", methods=["POST"])
def get_intent():
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    digit = request.values.get("Digits", "")
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    caller = request.args.get("caller", "Unknown")
    retries = int(request.args.get("retries", 0))

    if wants_agent(speech, digit):
        return transfer_to_agent(response, name)

    intent = clean_text(speech)

    if not intent:
        if retries >= MAX_RETRIES:
            say(response, "Let me connect you with someone who can help.")
            return transfer_to_agent(response, name)
        retry_url = build_url("/get_intent", name=name, service=service, caller=caller, retries=retries + 1)
        gather = gather_speech(retry_url)
        say(gather, "I didn't catch that. Could you briefly describe what you need help with? Or press zero for a team member.")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url("/get_urgency", name=name, service=service, intent=intent, caller=caller)
    gather = gather_speech(next_url)
    say(gather, "Thank you. Is this an urgent or emergency situation? Please say yes or no.")
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_urgency", methods=["POST"])
def get_urgency():
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    digit = request.values.get("Digits", "")
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    caller = request.args.get("caller", "Unknown")
    retries = int(request.args.get("retries", 0))

    if wants_agent(speech, digit):
        return transfer_to_agent(response, name)

    answer = yes_no_answer(speech)

    if answer == "yes":
        urgency = "Urgent"
        # Send immediate urgent alert to business owner
        send_urgent_alert(name, caller, service, intent)
    elif answer == "no":
        urgency = "Not Urgent"
    else:
        if retries >= MAX_RETRIES:
            say(response, "Let me connect you with someone who can help.")
            return transfer_to_agent(response, name)
        retry_url = build_url("/get_urgency", name=name, service=service, intent=intent, caller=caller, retries=retries + 1)
        gather = gather_speech(retry_url)
        say(gather, "Sorry, please say yes if it is urgent or no if it is not. Or press zero for a team member.")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url("/get_details", name=name, service=service, intent=intent, urgency=urgency, caller=caller)
    gather = gather_speech(next_url)
    say(gather, "Got it. Finally, please share any additional details you would like us to know.")
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_details", methods=["POST"])
def get_details():
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    digit = request.values.get("Digits", "")
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    urgency = request.args.get("urgency", "")
    caller = request.args.get("caller", "Unknown")

    if wants_agent(speech, digit):
        return transfer_to_agent(response, name)

    details = clean_text(speech)
    if not details:
        details = "No extra details provided"

    append_to_csv(name, caller, service, intent, urgency, details)
    send_lead_alert(name, caller, service, urgency, intent)
    send_booking_sms(caller, name, service)

    say(response, (
        f"Thank you {name}. We have received your request for {service}. "
        f"I am sending a text message to your phone right now with a link to book your appointment. "
        f"Please check your messages. We look forward to helping you. Have a great day. Goodbye."
    ))
    response.hangup()
    return str(response)


@app.route("/call_ended", methods=["POST"])
def call_ended():
    """Called after a transferred call ends."""
    response = VoiceResponse()
    return str(response)


@app.route("/voicemail_saved", methods=["POST"])
def voicemail_saved():
    """Called after a voicemail is recorded."""
    response = VoiceResponse()
    say(response, "Your message has been saved. We will get back to you as soon as possible. Goodbye.")
    response.hangup()
    return str(response)


@app.route("/check-csv", methods=["GET"])
def check_csv():
    token = request.args.get("token", "")
    expected = os.environ.get("DASHBOARD_TOKEN", "")
    if expected and token != expected:
        return "Unauthorized", 401
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return f"<pre>{f.read()}</pre>"
    return "CSV file not found.", 404


@app.route("/download-csv", methods=["GET"])
def download_csv():
    token = request.args.get("token", "")
    expected = os.environ.get("DASHBOARD_TOKEN", "")
    if expected and token != expected:
        return "Unauthorized", 401
    if os.path.exists(DATA_FILE):
        return send_file(DATA_FILE, as_attachment=True)
    return "CSV file not found.", 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
