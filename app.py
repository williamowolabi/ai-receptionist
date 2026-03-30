from flask import Flask, request, send_file, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
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

# In-memory cache
text_cache = {}
audio_cache = {}

# Hints help Twilio recognize expected words and ignore noise
GENERAL_HINTS = (
    "yes, no, yeah, nope, plumbing, HVAC, electrical, roofing, "
    "landscaping, painting, flooring, urgent, not urgent, "
    "appointment, schedule, help, repair, install, replace, fix, "
    "leak, broken, damage, emergency, inspection"
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
    """
    Gather speech with noise filtering enabled.
    - enhanced=True: uses Twilio's better noise-filtering AI
    - speech_model="phone_call": tuned for phone audio
    - hints: tells Twilio what words to expect, reduces misheard noise
    """
    return Gather(
        input="speech",
        action=action_url,
        method="POST",
        speech_timeout="auto",
        language=LANGUAGE,
        enhanced=True,
        speech_model="phone_call",
        hints=hints,
        profanity_filter=False
    )


def build_url(path, **params):
    if not params:
        return path
    return f"{path}?{urlencode(params)}"


def clean_text(value):
    """Clean and validate speech input — ignore noise and very short captures."""
    if not value:
        return ""
    value = value.strip()
    # Ignore captures under 3 characters — likely background noise
    if len(value) < 3:
        return ""
    # Ignore captures that are just punctuation or silence artifacts
    if all(c in ".,!?- " for c in value):
        return ""
    return value


def yes_no_answer(text):
    t = clean_text(text).lower()
    if any(w in t for w in ["yes", "yeah", "yep", "correct", "right", "affirmative", "sure", "absolutely"]):
        return "yes"
    if any(w in t for w in ["no", "nope", "nah", "incorrect", "wrong", "negative"]):
        return "no"
    return ""


def send_booking_sms(to_number, name, service):
    """Send Calendly booking link via SMS after the call ends."""
    try:
        if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
            print("Twilio credentials missing — skipping SMS")
            return
        if not to_number or to_number == "Unknown":
            print("No valid phone number — skipping SMS")
            return

        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        body = (
            f"Hi {name}, thanks for calling!\n\n"
            f"To book your {service} appointment click below:\n"
            f"{CALENDLY_LINK}\n\n"
            f"We look forward to helping you!"
        )
        client.messages.create(body=body, from_=TWILIO_PHONE_NUMBER, to=to_number)
        print(f"SMS sent to {to_number}")
    except Exception as e:
        print(f"SMS failed: {e}")


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
    gather = gather_speech("/get_name", hints="my name is, first name, last name")
    say(gather, "Thank you for calling. You've reached the service desk. What is your full name please?")
    response.append(gather)
    response.redirect("/voice")
    return str(response)


@app.route("/get_name", methods=["POST"])
def get_name():
    response = VoiceResponse()
    name = clean_text(request.values.get("SpeechResult"))
    caller = request.values.get("From", "Unknown")

    if not name:
        gather = gather_speech("/get_name", hints="my name is, first name, last name")
        say(gather, "I didn't catch your name. Could you please say your full name?")
        response.append(gather)
        response.redirect("/voice")
        return str(response)

    next_url = build_url("/get_service", name=name, caller=caller)
    gather = gather_speech(next_url, hints=GENERAL_HINTS)
    say(gather, f"Thanks {name}. What type of service do you need today? For example, plumbing, HVAC, electrical, or roofing.")
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_service", methods=["POST"])
def get_service():
    response = VoiceResponse()
    service = clean_text(request.values.get("SpeechResult"))
    name = request.args.get("name", "")
    caller = request.args.get("caller", "Unknown")

    if not service:
        retry_url = build_url("/get_service", name=name, caller=caller)
        gather = gather_speech(retry_url, hints=GENERAL_HINTS)
        say(gather, "I didn't catch that. Please tell me the type of service you need, like plumbing, HVAC, or electrical.")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    confirm_url = build_url("/confirm_service", name=name, service=service, caller=caller)
    gather = gather_speech(confirm_url, hints="yes, no, yeah, nope, correct, wrong")
    say(gather, f"Got it, you need {service}. Is that correct? Please say yes or no.")
    response.append(gather)
    response.redirect(confirm_url)
    return str(response)


@app.route("/confirm_service", methods=["POST"])
def confirm_service():
    response = VoiceResponse()
    answer = yes_no_answer(request.values.get("SpeechResult"))
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    caller = request.args.get("caller", "Unknown")

    if answer == "yes":
        next_url = build_url("/get_intent", name=name, service=service, caller=caller)
        gather = gather_speech(next_url, hints=GENERAL_HINTS)
        say(gather, "Great. Can you briefly describe what's going on and what you need help with?")
        response.append(gather)
        response.redirect(next_url)
        return str(response)

    if answer == "no":
        next_url = build_url("/get_service", name=name, caller=caller)
        gather = gather_speech(next_url, hints=GENERAL_HINTS)
        say(gather, "No problem, let's try again. What type of service do you need?")
        response.append(gather)
        response.redirect(next_url)
        return str(response)

    retry_url = build_url("/confirm_service", name=name, service=service, caller=caller)
    gather = gather_speech(retry_url, hints="yes, no, yeah, nope")
    say(gather, "Sorry, I didn't catch that. Please say yes or no.")
    response.append(gather)
    response.redirect(retry_url)
    return str(response)


@app.route("/get_intent", methods=["POST"])
def get_intent():
    response = VoiceResponse()
    intent = clean_text(request.values.get("SpeechResult"))
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    caller = request.args.get("caller", "Unknown")

    if not intent:
        retry_url = build_url("/get_intent", name=name, service=service, caller=caller)
        gather = gather_speech(retry_url, hints=GENERAL_HINTS)
        say(gather, "I didn't catch that. Could you briefly describe what you need help with?")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url("/get_urgency", name=name, service=service, intent=intent, caller=caller)
    gather = gather_speech(next_url, hints="yes, no, urgent, not urgent, emergency")
    say(gather, "Thank you. Is this an urgent or emergency situation? Please say yes or no.")
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_urgency", methods=["POST"])
def get_urgency():
    response = VoiceResponse()
    answer = yes_no_answer(request.values.get("SpeechResult"))
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    caller = request.args.get("caller", "Unknown")

    if answer == "yes":
        urgency = "Urgent"
    elif answer == "no":
        urgency = "Not Urgent"
    else:
        retry_url = build_url("/get_urgency", name=name, service=service, intent=intent, caller=caller)
        gather = gather_speech(retry_url, hints="yes, no, urgent, not urgent")
        say(gather, "Sorry, please say yes if it is urgent or no if it is not.")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url("/get_details", name=name, service=service, intent=intent, urgency=urgency, caller=caller)
    gather = gather_speech(next_url, hints=GENERAL_HINTS)
    say(gather, "Got it. Finally, please share any additional details you would like us to know.")
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_details", methods=["POST"])
def get_details():
    response = VoiceResponse()
    details = clean_text(request.values.get("SpeechResult"))
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    urgency = request.args.get("urgency", "")
    caller = request.args.get("caller", "Unknown")

    if not details:
        details = "No extra details provided"

    append_to_csv(name, caller, service, intent, urgency, details)
    send_booking_sms(caller, name, service)

    say(response, (
        f"Thank you {name}. We have received your request for {service}. "
        f"I am sending a text message to your phone right now with a link to book your appointment. "
        f"Please check your messages. We look forward to helping you. Have a great day. Goodbye."
    ))
    response.hangup()
    return str(response)


@app.route("/check-csv", methods=["GET"])
def check_csv():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return f"<pre>{f.read()}</pre>"
    return "CSV file not found.", 404


@app.route("/download-csv", methods=["GET"])
def download_csv():
    if os.path.exists(DATA_FILE):
        return send_file(DATA_FILE, as_attachment=True)
    return "CSV file not found.", 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
