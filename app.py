#!/usr/bin/env python
# coding: utf-8

# In[1]:

from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from urllib.parse import quote
import os

app = Flask(__name__)

# Better: use environment variables instead of hardcoding secrets
account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
twilio_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
your_phone = os.getenv("YOUR_PHONE_NUMBER", "").strip()

client = Client(account_sid, auth_token)


# STEP 1: Greeting - ask what they need first
@app.route("/voice", methods=["POST", "GET"])
def voice():
    response = VoiceResponse()

    gather = Gather(
        input="speech",
        action="/get_service",
        method="POST",
        speech_timeout="auto",
        timeout=5
    )
    gather.say("Hello. Thanks for calling. How can I help you today?")
    response.append(gather)

    response.say("Sorry, I didn’t catch that. Please call again.")
    return str(response)


# STEP 2: Capture service/problem first
@app.route("/get_service", methods=["POST", "GET"])
def get_service():
    service = request.form.get("SpeechResult", "").strip() or "service needed"
    safe_service = quote(service)

    print("CAPTURED SERVICE:", service)

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/get_name?service={safe_service}",
        method="POST",
        speech_timeout="auto",
        timeout=5
    )
    gather.say("Got it. And can I get your name?")
    response.append(gather)

    response.say("Sorry, I didn’t catch your name.")
    return str(response)


# STEP 3: Capture name second
@app.route("/get_name", methods=["POST", "GET"])
def get_name():
    service = request.args.get("service", "service needed")
    name = request.form.get("SpeechResult", "").strip() or "customer"

    safe_name = quote(name)
    safe_service = quote(service)

    print("CAPTURED NAME:", name)
    print("SERVICE SO FAR:", service)

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/get_details?name={safe_name}&service={safe_service}",
        method="POST",
        speech_timeout="auto",
        timeout=5
    )
    gather.say(f"Thanks {name}. Can you tell me a little more about what you need?")
    response.append(gather)

    response.say("Sorry, I didn’t catch that.")
    return str(response)


# STEP 4: Capture extra details, then send SMS
@app.route("/get_details", methods=["POST", "GET"])
def get_details():
    name = request.args.get("name", "customer")
    service = request.args.get("service", "a service")
    details = request.form.get("SpeechResult", "").strip() or "not specified"

    print("FINAL NAME:", name)
    print("FINAL SERVICE:", service)
    print("FINAL DETAILS:", details)

    response = VoiceResponse()

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
        f"Thanks {name}. Just to confirm, you need help with {service}. "
        f"You said: {details}. "
        "I’ve got everything I need. Someone will reach out to you shortly."
    )

    return str(response)


import os

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
