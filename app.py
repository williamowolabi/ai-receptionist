#!/usr/bin/env python
# coding: utf-8

# In[1]:from flask import Flask, request, send_file
from flask import Flask, request, send_file
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from urllib.parse import quote
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import csv

app = Flask(__name__)

@app.route("/")
def home():
    return "AI Receptionist is live"


account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
twilio_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
your_phone = os.getenv("YOUR_PHONE_NUMBER", "").strip()

client = Client(account_sid, auth_token) if account_sid and auth_token else None

# Local + Render-safe CSV path
DATA_FILE = os.getenv("DATA_FILE", os.path.join(os.getcwd(), "calls.csv"))

# Voice settings
AI_VOICE = "Polly.Joanna-Generative"
AI_LANGUAGE = "en-US"


def clean_speech():
    return request.form.get("SpeechResult", "").strip()


def say_gather(gather, text):
    gather.say(text, voice=AI_VOICE, language=AI_LANGUAGE)


def say_response(response, text):
    response.say(text, voice=AI_VOICE, language=AI_LANGUAGE)


def normalize_service(service):
    service = service.strip()

    prefixes = [
        "i need ",
        "i need a ",
        "i need an ",
        "i'm calling about ",
        "im calling about ",
        "calling about ",
        "i have a ",
        "i have an ",
        "i have ",
        "need ",
        "looking for ",
        "i'm looking for ",
        "im looking for "
    ]

    lower_service = service.lower()
    for prefix in prefixes:
        if lower_service.startswith(prefix):
            return service[len(prefix):].strip()

    return service


def ensure_csv_exists():
    folder = os.path.dirname(DATA_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)

    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "call_time",
                "caller_phone",
                "service",
                "customer_name",
                "details"
            ])


def save_call(caller_phone, service, customer_name, details):
    ensure_csv_exists()
    call_time = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")

    with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            call_time,
            caller_phone,
            service,
            customer_name,
            details
        ])


@app.route("/download-calls")
def download_calls():
    ensure_csv_exists()
    return send_file(DATA_FILE, as_attachment=True)


# STEP 1: Greeting
@app.route("/voice", methods=["POST", "GET"])
def voice():
    response = VoiceResponse()

    gather = Gather(
        input="speech",
        action="/confirm_service",
        method="POST",
        speech_timeout="auto",
        timeout=5
    )
    say_gather(gather, "Hello, thanks for calling. How can I help you today?")
    response.append(gather)

    say_response(response, "Sorry, I didn’t catch that. Please call again.")
    return str(response)


# STEP 2: Confirm service
@app.route("/confirm_service", methods=["POST", "GET"])
def confirm_service():
    raw_service = clean_speech() or "service needed"
    service = normalize_service(raw_service) or "service needed"
    safe_service = quote(service)

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/handle_service_confirmation?service={safe_service}",
        method="POST",
        speech_timeout="auto",
        timeout=5
    )
    say_gather(gather, f"Just to confirm, you need {service}. Is that right?")
    response.append(gather)

    say_response(response, "Sorry, I didn’t catch that. Please say yes or no.")
    return str(response)


@app.route("/handle_service_confirmation", methods=["POST", "GET"])
def handle_service_confirmation():
    service = request.args.get("service", "service needed")
    confirmation = clean_speech().lower()
    response = VoiceResponse()

    if "yes" in confirmation:
        safe_service = quote(service)
        gather = Gather(
            input="speech",
            action=f"/confirm_name?service={safe_service}",
            method="POST",
            speech_timeout="auto",
            timeout=5
        )
        say_gather(gather, "Got it. Can I get your name?")
        response.append(gather)

        say_response(response, "Sorry, I didn’t catch your name.")
        return str(response)

    elif "no" in confirmation:
        gather = Gather(
            input="speech",
            action="/confirm_service",
            method="POST",
            speech_timeout="auto",
            timeout=5
        )
        say_gather(gather, "Okay, let’s try that again. What service do you need?")
        response.append(gather)

        say_response(response, "Sorry, I didn’t catch that.")
        return str(response)

    else:
        safe_service = quote(service)
        gather = Gather(
            input="speech",
            action=f"/handle_service_confirmation?service={safe_service}",
            method="POST",
            speech_timeout="auto",
            timeout=5
        )
        say_gather(gather, "Please say yes or no.")
        response.append(gather)

        say_response(response, "Sorry, I didn’t catch that.")
        return str(response)


# STEP 3: Confirm name
@app.route("/confirm_name", methods=["POST", "GET"])
def confirm_name():
    service = request.args.get("service", "service needed")
    name = clean_speech() or "customer"

    safe_service = quote(service)
    safe_name = quote(name)

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/handle_name_confirmation?service={safe_service}&name={safe_name}",
        method="POST",
        speech_timeout="auto",
        timeout=5
    )
    say_gather(gather, f"You said your name is {name}. Is that correct?")
    response.append(gather)

    say_response(response, "Sorry, I didn’t catch that. Please say yes or no.")
    return str(response)


@app.route("/handle_name_confirmation", methods=["POST", "GET"])
def handle_name_confirmation():
    service = request.args.get("service", "service needed")
    name = request.args.get("name", "customer")
    confirmation = clean_speech().lower()
    response = VoiceResponse()

    if "yes" in confirmation:
        safe_service = quote(service)
        safe_name = quote(name)

        gather = Gather(
            input="speech",
            action=f"/confirm_details?service={safe_service}&name={safe_name}",
            method="POST",
            speech_timeout="auto",
            timeout=5
        )
        say_gather(gather, f"Thanks, {name}. Can you tell me a little more about what you need?")
        response.append(gather)

        say_response(response, "Sorry, I didn’t catch that.")
        return str(response)

    elif "no" in confirmation:
        safe_service = quote(service)

        gather = Gather(
            input="speech",
            action=f"/confirm_name?service={safe_service}",
            method="POST",
            speech_timeout="auto",
            timeout=5
        )
        say_gather(gather, "Okay, let’s try again. Please say your name.")
        response.append(gather)

        say_response(response, "Sorry, I didn’t catch that.")
        return str(response)

    else:
        safe_service = quote(service)
        safe_name = quote(name)

        gather = Gather(
            input="speech",
            action=f"/handle_name_confirmation?service={safe_service}&name={safe_name}",
            method="POST",
            speech_timeout="auto",
            timeout=5
        )
        say_gather(gather, "Please say yes or no.")
        response.append(gather)

        say_response(response, "Sorry, I didn’t catch that.")
        return str(response)


# STEP 4: Confirm details
@app.route("/confirm_details", methods=["POST", "GET"])
def confirm_details():
    service = request.args.get("service", "service needed")
    name = request.args.get("name", "customer")
    details = clean_speech() or "not specified"

    safe_service = quote(service)
    safe_name = quote(name)
    safe_details = quote(details)

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/handle_details_confirmation?service={safe_service}&name={safe_name}&details={safe_details}",
        method="POST",
        speech_timeout="auto",
        timeout=5
    )
    say_gather(gather, f"Just to confirm, you said {details}. Is that correct?")
    response.append(gather)

    say_response(response, "Sorry, I didn’t catch that. Please say yes or no.")
    return str(response)


@app.route("/handle_details_confirmation", methods=["POST", "GET"])
def handle_details_confirmation():
    service = request.args.get("service", "service needed")
    name = request.args.get("name", "customer")
    details = request.args.get("details", "not specified")
    confirmation = clean_speech().lower()

    response = VoiceResponse()

    if "yes" in confirmation:
        caller_phone = request.form.get("From", "unknown")

        save_call(caller_phone, service, name, details)

        message_body = (
            f"New Lead:\n"
            f"Time: {datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Name: {name}\n"
            f"Phone: {caller_phone}\n"
            f"Service: {service}\n"
            f"Details: {details}"
        )

        try:
            if client and twilio_number and your_phone:
                client.messages.create(
                    body=message_body,
                    from_=twilio_number,
                    to=your_phone
                )
            else:
                print("SMS skipped: missing Twilio environment variables")
        except Exception as e:
            print("SMS ERROR:", e)

        say_response(
            response,
            f"Thanks, {name}. I’ve got everything I need. Someone will reach out to you shortly."
        )
        return str(response)

    elif "no" in confirmation:
        safe_service = quote(service)
        safe_name = quote(name)

        gather = Gather(
            input="speech",
            action=f"/confirm_details?service={safe_service}&name={safe_name}",
            method="POST",
            speech_timeout="auto",
            timeout=5
        )
        say_gather(gather, "Okay, let’s try again. Please tell me a little more about what you need.")
        response.append(gather)

        say_response(response, "Sorry, I didn’t catch that.")
        return str(response)

    else:
        safe_service = quote(service)
        safe_name = quote(name)
        safe_details = quote(details)

        gather = Gather(
            input="speech",
            action=f"/handle_details_confirmation?service={safe_service}&name={safe_name}&details={safe_details}",
            method="POST",
            speech_timeout="auto",
            timeout=5
        )
        say_gather(gather, "Please say yes or no.")
        response.append(gather)

        say_response(response, "Sorry, I didn’t catch that.")
        return str(response)


if __name__ == "__main__":
    ensure_csv_exists()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
