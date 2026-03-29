from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Gather
import os
import csv
from datetime import datetime
from urllib.parse import urlencode

app = Flask(__name__)

# Render persistent disk path; local fallback for your Mac
DATA_FILE = "/var/data/calls.csv" if os.path.exists("/var/data") else "calls.csv"

# Better-sounding Twilio voice options are supported on <Say>.
# You can swap this later if you want a different sound.
VOICE = "Polly.Joanna"
LANGUAGE = "en-US"


def say_text(response, text):
    """Speak text with a chosen voice."""
    response.say(text, voice=VOICE, language=LANGUAGE)


def gather_speech(action_url):
    """Create a speech gather with solid defaults."""
    return Gather(
        input="speech",
        action=action_url,
        method="POST",
        speech_timeout="auto",
        language=LANGUAGE
    )


def build_url(path, **params):
    """Safely build URLs with encoded query parameters."""
    if not params:
        return path
    return f"{path}?{urlencode(params)}"


def clean_text(value):
    if not value:
        return ""
    return value.strip()


def yes_no_answer(text):
    """Normalize common yes/no variations."""
    t = clean_text(text).lower()
    if any(word in t for word in ["yes", "yeah", "yep", "correct", "right", "affirmative"]):
        return "yes"
    if any(word in t for word in ["no", "nope", "nah", "incorrect", "wrong"]):
        return "no"
    return ""


def ensure_csv_exists():
    folder = os.path.dirname(DATA_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)

    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "caller",
                "service",
                "intent",
                "urgency",
                "details"
            ])


def append_to_csv(caller, service, intent, urgency, details):
    with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            caller,
            service,
            intent,
            urgency,
            details
        ])


@app.route("/", methods=["GET"])
def home():
    return "AI Receptionist is running."


@app.route("/voice", methods=["GET", "POST"])
def voice():
    response = VoiceResponse()

    gather = gather_speech("/get_service")
    gather.say(
        "Thank you for calling. You’ve reached the service desk. "
        "Please tell me what type of service you need today, "
        "like plumbing, HVAC, electrical, roofing, or something else.",
        voice=VOICE,
        language=LANGUAGE
    )
    response.append(gather)

    response.redirect("/voice")
    return str(response)


@app.route("/get_service", methods=["POST"])
def get_service():
    response = VoiceResponse()

    service = clean_text(request.values.get("SpeechResult"))
    caller = request.values.get("From", "Unknown")

    if not service:
        gather = gather_speech("/get_service")
        gather.say(
            "I didn’t catch that. Please tell me the type of service you need.",
            voice=VOICE,
            language=LANGUAGE
        )
        response.append(gather)
        response.redirect("/voice")
        return str(response)

    confirm_url = build_url("/confirm_service", service=service, caller=caller)

    gather = gather_speech(confirm_url)
    gather.say(
        f"Just to make sure I heard you right, you need {service}. Please say yes or no.",
        voice=VOICE,
        language=LANGUAGE
    )
    response.append(gather)

    response.redirect(confirm_url)
    return str(response)


@app.route("/confirm_service", methods=["POST"])
def confirm_service():
    response = VoiceResponse()

    answer = yes_no_answer(request.values.get("SpeechResult"))
    service = request.args.get("service", "")
    caller = request.args.get("caller", "Unknown")

    if answer == "yes":
        next_url = build_url("/get_intent", service=service, caller=caller)
        gather = gather_speech(next_url)
        gather.say(
            "Great. Please briefly tell me what is going on and what you need help with.",
            voice=VOICE,
            language=LANGUAGE
        )
        response.append(gather)
        response.redirect(next_url)
        return str(response)

    if answer == "no":
        gather = gather_speech("/get_service")
        gather.say(
            "Okay, let’s try that again. Please tell me the type of service you need.",
            voice=VOICE,
            language=LANGUAGE
        )
        response.append(gather)
        response.redirect("/voice")
        return str(response)

    retry_url = build_url("/confirm_service", service=service, caller=caller)
    gather = gather_speech(retry_url)
    gather.say(
        "Please say yes or no. Do you need that service?",
        voice=VOICE,
        language=LANGUAGE
    )
    response.append(gather)
    response.redirect(retry_url)
    return str(response)


@app.route("/get_intent", methods=["POST"])
def get_intent():
    response = VoiceResponse()

    intent = clean_text(request.values.get("SpeechResult"))
    service = request.args.get("service", "")
    caller = request.args.get("caller", "Unknown")

    if not intent:
        retry_url = build_url("/get_intent", service=service, caller=caller)
        gather = gather_speech(retry_url)
        gather.say(
            "I didn’t catch that. Please briefly tell me what you need help with.",
            voice=VOICE,
            language=LANGUAGE
        )
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url("/get_urgency", service=service, intent=intent, caller=caller)
    gather = gather_speech(next_url)
    gather.say(
        "Thank you. Is this urgent? Please say yes or no.",
        voice=VOICE,
        language=LANGUAGE
    )
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_urgency", methods=["POST"])
def get_urgency():
    response = VoiceResponse()

    answer = yes_no_answer(request.values.get("SpeechResult"))
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    caller = request.args.get("caller", "Unknown")

    if answer == "yes":
        urgency = "Urgent"
    elif answer == "no":
        urgency = "Not Urgent"
    else:
        retry_url = build_url("/get_urgency", service=service, intent=intent, caller=caller)
        gather = gather_speech(retry_url)
        gather.say(
            "Please say yes or no. Is this urgent?",
            voice=VOICE,
            language=LANGUAGE
        )
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url(
        "/get_details",
        service=service,
        intent=intent,
        urgency=urgency,
        caller=caller
    )
    gather = gather_speech(next_url)
    gather.say(
        "Got it. Please share any extra details you want us to know.",
        voice=VOICE,
        language=LANGUAGE
    )
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_details", methods=["POST"])
def get_details():
    response = VoiceResponse()

    details = clean_text(request.values.get("SpeechResult"))
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    urgency = request.args.get("urgency", "")
    caller = request.args.get("caller", "Unknown")

    if not details:
        details = "No extra details provided"

    append_to_csv(caller, service, intent, urgency, details)

    say_text(
        response,
        f"Thank you. I have your request for {service}. "
        "We’ve saved your information and someone will follow up with you soon. Goodbye."
    )
    response.hangup()
    return str(response)


if __name__ == "__main__":
    ensure_csv_exists()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
