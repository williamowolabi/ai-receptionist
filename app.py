from flask import Flask, request, send_file, Response
from twilio.twiml.voice_response import VoiceResponse, Gather, Record
from twilio.rest import Client
from openai import OpenAI
import os
import csv
import hashlib
import threading
import tempfile
import requests
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
OWNER_PHONE = os.environ.get("OWNER_PHONE", "")

# --- MAX RETRIES PER STEP ---
MAX_RETRIES = 3

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

# Pre-warm ALL phrases the AI says on every call at startup
PREWARM_PHRASES = [
    "Thank you for calling. You've reached the service desk. What is your full name please?",
    "I didn't catch your name. Could you please say your full name?",
    "I'm sorry I'm having trouble hearing you. Please call back and we will be happy to help. Goodbye.",
    "What type of service do you need today? For example, plumbing, HVAC, electrical, or roofing.",
    "I didn't catch that. Please tell me the type of service you need, like plumbing, HVAC, or electrical.",
    "Is that correct? Please say yes or no.",
    "No problem, let's try again. What type of service do you need?",
    "Sorry, I didn't catch that. Please say yes or no.",
    "Great. Can you briefly describe what is going on and what you need help with?",
    "I didn't catch that. Could you briefly describe what you need help with?",
    "Thank you. Is this an urgent or emergency situation? Please say yes or no.",
    "Sorry, please say yes if it is urgent or no if it is not.",
    "Got it. Finally, please share any additional details you would like us to know.",
    "Thank you. One last thing — what is the best mobile number to send your booking link to? Please say your ten digit number.",
    "I didn't catch that number. Could you please say your ten digit mobile number?",
    "I'm sorry I was unable to get your number. We will follow up with you soon. Goodbye.",
    "Please call back and we will be happy to help. Goodbye.",
]


# ==============================================================================
# WHISPER TRANSCRIPTION
# Replaces Twilio's built-in speech recognition with OpenAI Whisper.
# Much more accurate for accents, background noise, and unusual names.
# ==============================================================================

def transcribe_with_whisper(recording_url):
    """
    Download a Twilio recording and transcribe it with OpenAI Whisper.
    Returns the transcribed text or empty string on failure.
    """
    if not openai_client:
        return ""
    try:
        # Download the audio from Twilio
        audio_response = requests.get(
            recording_url + ".mp3",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=10
        )
        if audio_response.status_code != 200:
            print(f"Failed to download recording: {audio_response.status_code}")
            return ""

        # Save to a temp file and send to Whisper
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio_response.content)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en"
            )

        os.unlink(tmp_path)
        result = transcript.text.strip()
        print(f"Whisper transcribed: {result}")
        return result

    except Exception as e:
        print(f"Whisper transcription error: {e}")
        return ""


# ==============================================================================
# GPT-4 CLEANUP LAYER
# Runs silently on every caller response.
# Cleans up messy speech, understands slang, extracts meaning accurately.
# Your call flow stays exactly the same — GPT just makes inputs smarter.
# ==============================================================================

def gpt_clean_name(raw_text):
    """
    Use GPT-4 to extract just the name from whatever the caller said.
    Handles: "uh my name is John Smith", "it's John", "Johnny Smith here"
    Falls back to rule-based extraction if GPT fails.
    """
    if not openai_client or not raw_text:
        return extract_name(raw_text)
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=20,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract only the person's name from the text. "
                        "Return just the name, properly capitalized. "
                        "If no name is found return empty string. "
                        "Examples: 'my name is john smith' -> 'John Smith', "
                        "'this is sarah' -> 'Sarah', 'uh hi its mike johnson' -> 'Mike Johnson'"
                    )
                },
                {"role": "user", "content": raw_text}
            ]
        )
        name = resp.choices[0].message.content.strip()
        if len(name) < 2 or len(name) > 50:
            return extract_name(raw_text)
        return name
    except Exception as e:
        print(f"GPT name extraction error: {e}")
        return extract_name(raw_text)


def gpt_clean_service(raw_text):
    """
    Use GPT-4 to identify the service type from natural speech.
    Handles slang: "AC is busted" -> "HVAC", "pipes are leaking" -> "Plumbing"
    Falls back to raw text if GPT fails.
    """
    if not openai_client or not raw_text:
        return clean_text(raw_text)
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=15,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Identify the home service type from the text. "
                        "Return one of: Plumbing, HVAC, Electrical, Roofing, Landscaping, "
                        "Painting, Flooring, General Handyman, or the closest match. "
                        "Examples: 'my AC is broken' -> 'HVAC', "
                        "'pipes are leaking' -> 'Plumbing', "
                        "'lights wont turn on' -> 'Electrical', "
                        "'my roof is leaking' -> 'Roofing'. "
                        "Return just the service name, nothing else."
                    )
                },
                {"role": "user", "content": raw_text}
            ]
        )
        service = resp.choices[0].message.content.strip()
        if len(service) < 2:
            return clean_text(raw_text)
        return service
    except Exception as e:
        print(f"GPT service extraction error: {e}")
        return clean_text(raw_text)


def gpt_summarize_lead(name, service, intent, urgency, details, caller):
    """
    Use GPT-4 to generate a clean professional lead summary
    for the business owner notification instead of raw transcribed text.
    """
    if not openai_client:
        return None
    try:
        prompt = (
            f"Name: {name}\n"
            f"Phone: {caller}\n"
            f"Service: {service}\n"
            f"Issue: {intent}\n"
            f"Urgency: {urgency}\n"
            f"Details: {details}"
        )
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=80,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Write a 1-2 sentence professional summary of this service call lead "
                        "for a home service business owner. Be concise and factual. "
                        "Include the most important details they need to know to follow up."
                    )
                },
                {"role": "user", "content": prompt}
            ]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"GPT summary error: {e}")
        return None


# ==============================================================================
# TTS
# ==============================================================================

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


def prewarm_audio():
    """Pre-generate audio for common phrases at startup to eliminate pauses."""
    if not openai_client or not APP_URL:
        print("Skipping audio pre-warm — OpenAI or APP_URL not configured")
        return
    print("Pre-warming audio cache...")
    for phrase in PREWARM_PHRASES:
        key = hashlib.md5(phrase.encode()).hexdigest()
        text_cache[key] = phrase
        audio_data = generate_speech(phrase)
        if audio_data:
            audio_cache[key] = audio_data
            print(f"Cached: {phrase[:50]}...")
    print("Audio pre-warm complete!")


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


# ==============================================================================
# SPEECH GATHERING
# ==============================================================================

def gather_speech(action_url, hints=GENERAL_HINTS):
    """
    Gather speech with maximum noise filtering.
    - enhanced=True: Twilio best speech recognition
    - speech_model=phone_call: tuned for phone audio
    - hints: tells Twilio what words to expect
    - speech_timeout=3: 3 seconds silence before processing
    - action_on_empty_result=True: always fires retry logic on noise
    """
    return Gather(
        input="speech",
        action=action_url,
        method="POST",
        speech_timeout="3",
        language=LANGUAGE,
        enhanced=True,
        speech_model="phone_call",
        hints=hints,
        profanity_filter=False,
        action_on_empty_result=True
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
    """Strip common name prefixes — fallback when GPT unavailable."""
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


def is_likely_landline(phone_number):
    """Detect if the caller is on a landline."""
    if not phone_number or phone_number == "Unknown":
        return True
    cleaned = phone_number.replace("+", "").replace("-", "").replace(" ", "")
    if not cleaned.startswith("1") or len(cleaned) != 11:
        return True
    return False


def parse_spoken_number(text):
    """Convert spoken phone number to digits."""
    if not text:
        return ""
    word_to_digit = {
        "zero": "0", "one": "1", "two": "2", "three": "3",
        "four": "4", "five": "5", "six": "6", "seven": "7",
        "eight": "8", "nine": "9", "oh": "0"
    }
    result = ""
    for word in text.lower().split():
        if word in word_to_digit:
            result += word_to_digit[word]
        elif word.isdigit():
            result += word
    digits = "".join(c for c in result if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return ""


# ==============================================================================
# SMS
# ==============================================================================

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


def send_lead_alert(name, caller, service, urgency, intent, details):
    """Alert the business owner a new lead came in with GPT summary."""
    if not OWNER_PHONE:
        return
    urgency_flag = "🚨 URGENT" if urgency == "Urgent" else "📋 Standard"

    # Try to get a GPT-generated clean summary
    summary = gpt_summarize_lead(name, service, intent, urgency, details, caller)

    body = (
        f"📞 New Lead — NextReceptionist\n\n"
        f"{urgency_flag}\n"
        f"Name: {name}\n"
        f"Service: {service}\n"
        f"Phone: {caller}\n"
    )
    if summary:
        body += f"\nSummary: {summary}\n"
    else:
        body += f"Issue: {intent}\n"

    body += "\nBooking link sent to customer."
    send_sms(OWNER_PHONE, body)


def send_urgent_alert(name, caller, service, intent):
    """Send immediate alert when customer marks call as urgent."""
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


# ==============================================================================
# CSV
# ==============================================================================

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
threading.Thread(target=prewarm_audio, daemon=True).start()


# ==============================================================================
# ROUTES
# ==============================================================================

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
    speech = request.values.get("SpeechResult", "")
    caller = request.values.get("From", "Unknown")
    retries = int(request.args.get("retries", 0))

    # GPT-4 extracts the name from whatever they said
    name = gpt_clean_name(speech) if speech else ""

    if not name:
        if retries >= MAX_RETRIES:
            say(response, "I'm sorry I'm having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url("/get_name", retries=retries + 1)
        gather = gather_speech(retry_url, hints="my name is, first name, last name")
        say(gather, "I didn't catch your name. Could you please say your full name?")
        response.append(gather)
        response.redirect(retry_url)
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
    speech = request.values.get("SpeechResult", "")
    name = request.args.get("name", "")
    caller = request.args.get("caller", "Unknown")
    retries = int(request.args.get("retries", 0))

    # GPT-4 identifies service type even from slang
    service = gpt_clean_service(speech) if speech else ""

    if not service:
        if retries >= MAX_RETRIES:
            say(response, "I'm sorry I'm having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url("/get_service", name=name, caller=caller, retries=retries + 1)
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
    speech = request.values.get("SpeechResult", "")
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    caller = request.args.get("caller", "Unknown")
    retries = int(request.args.get("retries", 0))

    answer = yes_no_answer(speech)

    if answer == "yes":
        next_url = build_url("/get_intent", name=name, service=service, caller=caller)
        gather = gather_speech(next_url, hints=GENERAL_HINTS)
        say(gather, "Great. Can you briefly describe what is going on and what you need help with?")
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

    if retries >= MAX_RETRIES:
        say(response, "I'm sorry I'm having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
        response.hangup()
        return str(response)

    retry_url = build_url("/confirm_service", name=name, service=service, caller=caller, retries=retries + 1)
    gather = gather_speech(retry_url, hints="yes, no, yeah, nope")
    say(gather, "Sorry, I didn't catch that. Please say yes or no.")
    response.append(gather)
    response.redirect(retry_url)
    return str(response)


@app.route("/get_intent", methods=["POST"])
def get_intent():
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    caller = request.args.get("caller", "Unknown")
    retries = int(request.args.get("retries", 0))

    intent = clean_text(speech)

    if not intent:
        if retries >= MAX_RETRIES:
            say(response, "I'm sorry I'm having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url("/get_intent", name=name, service=service, caller=caller, retries=retries + 1)
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
    speech = request.values.get("SpeechResult", "")
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    caller = request.args.get("caller", "Unknown")
    retries = int(request.args.get("retries", 0))

    answer = yes_no_answer(speech)

    if answer == "yes":
        urgency = "Urgent"
        send_urgent_alert(name, caller, service, intent)
    elif answer == "no":
        urgency = "Not Urgent"
    else:
        if retries >= MAX_RETRIES:
            say(response, "I'm sorry I'm having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url("/get_urgency", name=name, service=service, intent=intent, caller=caller, retries=retries + 1)
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
    speech = request.values.get("SpeechResult", "")
    name = request.args.get("name", "")
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    urgency = request.args.get("urgency", "")
    caller = request.args.get("caller", "Unknown")

    details = clean_text(speech)
    if not details:
        details = "No extra details provided"

    # Check if caller is on a landline
    if is_likely_landline(caller):
        next_url = build_url(
            "/get_mobile",
            name=name, caller=caller, service=service,
            intent=intent, urgency=urgency, details=details
        )
        gather = gather_speech(next_url, hints="my number is, cell, mobile")
        say(gather, "Thank you. One last thing — what is the best mobile number to send your booking link to? Please say your ten digit number.")
        response.append(gather)
        response.redirect(next_url)
        return str(response)

    append_to_csv(name, caller, service, intent, urgency, details)
    send_lead_alert(name, caller, service, urgency, intent, details)
    send_booking_sms(caller, name, service)

    say(response, (
        f"Thank you {name}. We have received your request for {service}. "
        f"I am sending a text message to your phone right now with a link to book your appointment. "
        f"Please check your messages. We look forward to helping you. Have a great day. Goodbye."
    ))
    response.hangup()
    return str(response)


@app.route("/get_mobile", methods=["POST"])
def get_mobile():
    """Ask landline callers for their mobile number to receive the SMS."""
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    name = request.args.get("name", "")
    caller = request.args.get("caller", "Unknown")
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    urgency = request.args.get("urgency", "")
    details = request.args.get("details", "No extra details provided")
    retries = int(request.args.get("retries", 0))

    mobile = parse_spoken_number(speech)

    if not mobile:
        if retries >= MAX_RETRIES:
            append_to_csv(name, caller, service, intent, urgency, details)
            send_lead_alert(name, caller, service, urgency, intent, details)
            say(response, "I'm sorry I was unable to get your number. We will follow up with you soon. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url(
            "/get_mobile",
            name=name, caller=caller, service=service,
            intent=intent, urgency=urgency, details=details,
            retries=retries + 1
        )
        gather = gather_speech(retry_url, hints="my number is, cell, mobile")
        say(gather, "I didn't catch that number. Could you please say your ten digit mobile number?")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    append_to_csv(name, mobile, service, intent, urgency, details)
    send_lead_alert(name, mobile, service, urgency, intent, details)
    send_booking_sms(mobile, name, service)

    say(response, (
        f"Perfect, thank you {name}. We have received your request for {service}. "
        f"I am sending a text message to {mobile[-4:]} right now with a link to book your appointment. "
        f"Please check your messages. We look forward to helping you. Have a great day. Goodbye."
    ))
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
