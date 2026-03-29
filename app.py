#!/usr/bin/env python
# coding: utf-8

# In[1]:from flask import Flask, request, send_file
from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Gather
import os
import csv
from datetime import datetime

app = Flask(__name__)

# On Render, /var/data exists if you mounted a persistent disk there.
# Locally, it will save calls.csv in your project folder.
DATA_FILE = "/var/data/calls.csv" if os.path.exists("/var/data") else "calls.csv"


def ensure_csv_exists():
    """Create the CSV file with headers if it does not already exist."""
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
    """Append one completed call row to the CSV."""
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


def clean_text(value):
    """Safely clean speech input."""
    if not value:
        return ""
    return value.strip()


@app.route("/", methods=["GET"])
def home():
    return "AI Receptionist is running."


@app.route("/voice", methods=["GET", "POST"])
def voice():
    """Initial incoming call route."""
    response = VoiceResponse()

    gather = Gather(
        input="speech",
        action="/get_service",
        method="POST",
        speech_timeout="auto"
    )
    gather.say(
        "Thank you for calling. Please tell me what kind of service you need today, "
        "such as plumbing, HVAC, electrical, roofing, or something else."
    )
    response.append(gather)

    response.redirect("/voice")
    return str(response)


@app.route("/get_service", methods=["POST"])
def get_service():
    """Capture the requested service and ask for yes/no confirmation."""
    response = VoiceResponse()

    service = clean_text(request.values.get("SpeechResult"))
    caller = request.values.get("From", "Unknown")

    if not service:
        gather = Gather(
            input="speech",
            action="/get_service",
            method="POST",
            speech_timeout="auto"
        )
        gather.say("I did not catch that. Please say the type of service you need.")
        response.append(gather)
        response.redirect("/voice")
        return str(response)

    gather = Gather(
        input="speech",
        action=f"/confirm_service?service={service}&caller={caller}",
        method="POST",
        speech_timeout="auto"
    )
    gather.say(f"Just to confirm, you need {service}. Is that correct? Please say yes or no.")
    response.append(gather)

    response.redirect(f"/get_service")
    return str(response)


@app.route("/confirm_service", methods=["POST"])
def confirm_service():
    """Handle yes/no confirmation for service."""
    response = VoiceResponse()

    answer = clean_text(request.values.get("SpeechResult")).lower()
    service = request.args.get("service", "")
    caller = request.args.get("caller", "Unknown")

    if "yes" in answer:
        gather = Gather(
            input="speech",
            action=f"/get_intent?service={service}&caller={caller}",
            method="POST",
            speech_timeout="auto"
        )
        gather.say("Great. Please briefly tell me what you need help with.")
        response.append(gather)
        response.redirect(f"/confirm_service?service={service}&caller={caller}")
        return str(response)

    if "no" in answer:
        gather = Gather(
            input="speech",
            action="/get_service",
            method="POST",
            speech_timeout="auto"
        )
        gather.say("No problem. Please say the type of service you need again.")
        response.append(gather)
        response.redirect("/voice")
        return str(response)

    gather = Gather(
        input="speech",
        action=f"/confirm_service?service={service}&caller={caller}",
        method="POST",
        speech_timeout="auto"
    )
    gather.say("Please say yes or no. Is that service correct?")
    response.append(gather)
    response.redirect(f"/confirm_service?service={service}&caller={caller}")
    return str(response)


@app.route("/get_intent", methods=["POST"])
def get_intent():
    """Capture what the caller needs help with."""
    response = VoiceResponse()

    intent = clean_text(request.values.get("SpeechResult"))
    service = request.args.get("service", "")
    caller = request.args.get("caller", "Unknown")

    if not intent:
        gather = Gather(
            input="speech",
            action=f"/get_intent?service={service}&caller={caller}",
            method="POST",
            speech_timeout="auto"
        )
        gather.say("I did not catch that. Please briefly tell me what you need help with.")
        response.append(gather)
        response.redirect(f"/get_intent?service={service}&caller={caller}")
        return str(response)

    gather = Gather(
        input="speech",
        action=f"/get_urgency?service={service}&intent={intent}&caller={caller}",
        method="POST",
        speech_timeout="auto"
    )
    gather.say("Is this urgent? Please say yes or no.")
    response.append(gather)

    response.redirect(f"/get_intent?service={service}&caller={caller}")
    return str(response)


@app.route("/get_urgency", methods=["POST"])
def get_urgency():
    """Capture urgency as yes/no."""
    response = VoiceResponse()

    answer = clean_text(request.values.get("SpeechResult")).lower()
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    caller = request.args.get("caller", "Unknown")

    if "yes" in answer:
        urgency = "Urgent"
    elif "no" in answer:
        urgency = "Not Urgent"
    else:
        gather = Gather(
            input="speech",
            action=f"/get_urgency?service={service}&intent={intent}&caller={caller}",
            method="POST",
            speech_timeout="auto"
        )
        gather.say("Please say yes or no. Is this urgent?")
        response.append(gather)
        response.redirect(f"/get_urgency?service={service}&intent={intent}&caller={caller}")
        return str(response)

    gather = Gather(
        input="speech",
        action=f"/get_details?service={service}&intent={intent}&urgency={urgency}&caller={caller}",
        method="POST",
        speech_timeout="auto"
    )
    gather.say("Please share any extra details you want us to know.")
    response.append(gather)

    response.redirect(f"/get_urgency?service={service}&intent={intent}&caller={caller}")
    return str(response)


@app.route("/get_details", methods=["POST"])
def get_details():
    """Capture extra details and save everything to CSV."""
    response = VoiceResponse()

    details = clean_text(request.values.get("SpeechResult"))
    service = request.args.get("service", "")
    intent = request.args.get("intent", "")
    urgency = request.args.get("urgency", "")
    caller = request.args.get("caller", "Unknown")

    if not details:
        details = "No extra details provided"

    append_to_csv(caller, service, intent, urgency, details)

    response.say(
        f"Thank you. We have your request for {service}. "
        "Someone will follow up with you soon. Goodbye."
    )
    response.hangup()
    return str(response)


if __name__ == "__main__":
    ensure_csv_exists()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
