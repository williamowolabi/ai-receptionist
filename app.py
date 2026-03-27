#!/usr/bin/env python
# coding: utf-8

# In[1]:

from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from urllib.parse import quote
import os

app = Flask(__name__)

@app.route("/")
def home():
    return "AI Receptionist is live"

account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
twilio_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
your_phone = os.getenv("YOUR_PHONE_NUMBER", "").strip()

client = Client(account_sid, auth_token)


def clean_speech():
    return request.form.get("SpeechResult", "").strip()


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
    gather.say("Hello. Thanks for calling. How can I help you today?")
    response.append(gather)

    response.say("Sorry, I didn’t catch that. Please call again.")
    return str(response)


# STEP 2: Confirm service
@app.route("/confirm_service", methods=["POST", "GET"])
def confirm_service():
    service = clean_speech() or "service needed"
    safe_service = quote(service)

    print("CAPTURED SERVICE:", service)

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/handle_service_confirmation?service={safe_service}",
        method="POST",
        speech_timeout="auto",
        timeout=5
    )
    gather.say(f"You said {service}. Is that correct? Please say yes or no.")
    response.append(gather)

    response.say("Sorry, I didn’t catch that. Please say yes or no.")
    return str(response)


@app.route("/handle_service_confirmation", methods=["POST", "GET"])
def handle_service_confirmation():
    service = request.args.get("service", "service needed")
    confirmation = clean_speech().lower()

    print("SERVICE CONFIRMATION:", confirmation)

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
        gather.say("Got it. And can I get your name?")
        response.append(gather)
        response.say("Sorry, I didn’t catch your name.")
        return str(response)

    elif "no" in confirmation:
        gather = Gather(
            input="speech",
            action="/confirm_service",
            method="POST",
            speech_timeout="auto",
            timeout=5
        )
        gather.say("Okay, let’s try again. How can I help you today?")
        response.append(gather)
        response.say("Sorry, I didn’t catch that.")
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
        gather.say("Please say yes or no.")
        response.append(gather)
        response.say("Sorry, I didn’t catch that.")
        return str(response)


# STEP 3: Confirm name
@app.route("/confirm_name", methods=["POST", "GET"])
def confirm_name():
    service = request.args.get("service", "service needed")
    name = clean_speech() or "customer"

    safe_service = quote(service)
    safe_name = quote(name)

    print("CAPTURED NAME:", name)

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/handle_name_confirmation?service={safe_service}&name={safe_name}",
        method="POST",
        speech_timeout="auto",
        timeout=5
    )
    gather.say(f"You said your name is {name}. Is that correct? Please say yes or no.")
    response.append(gather)

    response.say("Sorry, I didn’t catch that. Please say yes or no.")
    return str(response)


@app.route("/handle_name_confirmation", methods=["POST", "GET"])
def handle_name_confirmation():
    service = request.args.get("service", "service needed")
    name = request.args.get("name", "customer")
    confirmation = clean_speech().lower()

    print("NAME CONFIRMATION:", confirmation)

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
        gather.say(f"Thanks {name}. Can you tell me a little more about what you need?")
        response.append(gather)

        response.say("Sorry, I didn’t catch that.")
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
        gather.say("Okay, let’s try again. Please say your name.")
        response.append(gather)

        response.say("Sorry, I didn’t catch that.")
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
        gather.say("Please say yes or no.")
        response.append(gather)

        response.say("Sorry, I didn’t catch that.")
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

    print("CAPTURED DETAILS:", details)

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/handle_details_confirmation?service={safe_service}&name={safe_name}&details={safe_details}",
        method="POST",
        speech_timeout="auto",
        timeout=5
    )
    gather.say(f"You said {details}. Is that correct? Please say yes or no.")
    response.append(gather)

    response.say("Sorry, I didn’t catch that. Please say yes or no.")
    return str(response)


@app.route("/handle_details_confirmation", methods=["POST", "GET"])
def handle_details_confirmation():
    service = request.args.get("service", "service needed")
    name = request.args.get("name", "customer")
    details = request.args.get("details", "not specified")
    confirmation = clean_speech().lower()

    print("DETAILS CONFIRMATION:", confirmation)

    response = VoiceResponse()

    if "yes" in confirmation:
        message_body = (
            f"New Lead:\n"
            f"Name: {name}\n"
            f"Service: {service}\n"
            f"Details: {details}"
        )

        try:
            client.messages.create(
                body=message_body,
                from_=twilio_number,
                to=your_phone
            )
            print("✅ SMS SENT")
        except Exception as e:
            print("❌ SMS ERROR:", e)

        response.say(
            f"Thanks {name}. I’ve got everything I need. "
            "Someone will reach out to you shortly."
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
        gather.say("Okay, let’s try again. Please tell me a little more about what you need.")
        response.append(gather)

        response.say("Sorry, I didn’t catch that.")
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
        gather.say("Please say yes or no.")
        response.append(gather)

        response.say("Sorry, I didn’t catch that.")
        return str(response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
