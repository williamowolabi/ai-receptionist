from flask import Flask, request, send_file, Response
from twilio.twiml.voice_response import VoiceResponse, Gather, Dial
from twilio.rest import Client
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
import atexit
import os
import csv
import hashlib
import threading
from datetime import datetime, timedelta
from urllib.parse import urlencode

app = Flask(__name__)

# ==============================================================================
# CONFIGURATION
# ==============================================================================

DATA_FILE           = "/var/data/calls.csv" if os.path.exists("/var/data") else "calls.csv"
LANGUAGE            = "en-US"
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY")
openai_client       = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
OPENAI_VOICE        = "nova"
TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
CALENDLY_LINK       = os.environ.get("CALENDLY_LINK", "https://calendly.com/your-link-here")
APP_URL             = os.environ.get("APP_URL", "").rstrip("/")
OWNER_PHONE         = os.environ.get("OWNER_PHONE", "")
MAX_RETRIES         = 3

# Audio caches
text_cache  = {}
audio_cache = {}

# GPT caches — avoids calling GPT twice for same input
gpt_name_cache    = {}
gpt_service_cache = {}

# Speech hints
GENERAL_HINTS = (
    "yes, no, plumbing, HVAC, electrical, roofing, landscaping, "
    "painting, flooring, urgent, not urgent, repair, install, "
    "replace, fix, leak, broken, damage, emergency, flooding, "
    "gas, fire, smoke, sparking, burst"
)

# Filler phrases — played instantly while GPT processes
FILLERS = [
    "One moment please.",
    "Sure, give me just a second.",
    "Got it, one moment.",
    "Jotting that down for you.",
    "Got it, jotting that down.",
]

# Pre-warm phrases — generated at startup so first call is instant
PREWARM_PHRASES = [
    "Thank you for calling. You've reached the service desk. "
    "If this is an emergency please say emergency now. "
    "Otherwise please tell us your name and we will be happy to help you.",
    "What is your full name please?",
    "I didn't catch your name. Could you please say your full name?",
    "I am having trouble hearing you. Please call back and we will be happy to help. Goodbye.",
    "What type of service do you need today? For example plumbing, HVAC, electrical, or roofing.",
    "I didn't catch that. Please tell me the type of service you need.",
    "Is that correct? Please say yes or no.",
    "No problem. What type of service do you need?",
    "Sorry, I didn't catch that. Please say yes or no.",
    "Great. Can you briefly describe what is going on and what you need help with?",
    "I didn't catch that. Could you briefly describe what you need help with?",
    "Thank you. Is this an urgent or emergency situation? Please say yes or no.",
    "Sorry, please say yes if it is urgent or no if it is not.",
    "Got it. Finally, please share any additional details you would like us to know.",
    "Thank you. One last thing. What is the best mobile number to send your booking link to?",
    "I didn't catch that number. Could you please say your ten digit mobile number?",
    "I understand this is an emergency. I am alerting our team right now. "
    "Please call 911 if you are in immediate danger. Someone will contact you within minutes. Goodbye.",
    "One moment please.",
    "Sure, give me just a second.",
    "Got it, one moment.",
    "Jotting that down for you.",
    "Got it, jotting that down.",
]

# ==============================================================================
# APSCHEDULER
# ==============================================================================

scheduler = BackgroundScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone="UTC"
)
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))

# ==============================================================================
# OPENAI TTS — nova voice only, consistent throughout
# ==============================================================================

def generate_speech(text):
    """Generate MP3 via OpenAI nova voice."""
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
        print("TTS error: " + str(e))
        return None


def prewarm_audio():
    """Pre-generate audio for all common phrases at startup."""
    if not openai_client or not APP_URL:
        print("Skipping pre-warm — OpenAI or APP_URL not set")
        return
    print("Pre-warming audio cache...")
    for phrase in PREWARM_PHRASES:
        key = hashlib.md5(phrase.encode()).hexdigest()
        text_cache[key] = phrase
        data = generate_speech(phrase)
        if data:
            audio_cache[key] = data
    print("Audio pre-warm complete!")


def say(response, text):
    """
    Play audio via OpenAI nova voice only.
    Checks memory cache first — repeated phrases never hit API again.
    Falls back to Polly only if OpenAI is not configured.
    """
    if openai_client and APP_URL:
        key = hashlib.md5(text.encode()).hexdigest()
        text_cache[key] = text
        response.play(APP_URL + "/audio/" + key)
    else:
        response.say(text, voice="Polly.Joanna", language=LANGUAGE)


def play_filler(response):
    """Play a filler phrase instantly while GPT processes."""
    import random
    filler = random.choice(FILLERS)
    if openai_client and APP_URL:
        key = hashlib.md5(filler.encode()).hexdigest()
        if key in audio_cache:
            text_cache[key] = filler
            response.play(APP_URL + "/audio/" + key)
            return
    response.say(filler, voice="Polly.Joanna", language=LANGUAGE)


@app.route("/audio/<key>", methods=["GET"])
def serve_audio(key):
    """Serve cached audio. Generate on demand if not yet cached."""
    text = text_cache.get(key)
    if not text:
        return "Not found", 404
    if key not in audio_cache:
        data = generate_speech(text)
        if not data:
            return "Generation failed", 500
        audio_cache[key] = data
    return Response(audio_cache[key], mimetype="audio/mpeg")


# Pre-warm at startup in background
threading.Thread(target=prewarm_audio, daemon=True).start()

# ==============================================================================
# SPEECH GATHERING
# ==============================================================================

def gather_speech(action_url, hints=GENERAL_HINTS, timeout="3"):
    return Gather(
        input="speech",
        action=action_url,
        method="POST",
        speech_timeout=timeout,
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
    return path + "?" + urlencode(params)


# ==============================================================================
# TEXT HELPERS
# ==============================================================================

def clean_text(value):
    if not value:
        return ""
    value = value.strip()
    if len(value) < 3:
        return ""
    if all(c in ".,!?- " for c in value):
        return ""
    return value


def yes_no_answer(text):
    t = clean_text(text).lower()
    if any(w in t for w in ["yes", "yeah", "yep", "correct", "right", "sure", "absolutely"]):
        return "yes"
    if any(w in t for w in ["no", "nope", "nah", "incorrect", "wrong", "negative"]):
        return "no"
    return ""


def is_emergency(text):
    """Instant keyword check — no GPT needed, fires in microseconds."""
    if not text:
        return False
    t = text.lower()
    keywords = [
        "gas leak", "gas smell", "smell gas", "flooding", "flood",
        "water everywhere", "pipe burst", "burst pipe", "house on fire",
        "fire in", "sparking", "electrical fire", "electrocuted",
        "exploded", "explosion", "collapse", "collapsed",
        "carbon monoxide", "not breathing", "cant breathe",
        "emergency"
    ]
    return any(k in t for k in keywords)


def is_likely_landline(phone_number):
    """Detect landline — ask for mobile number to send SMS."""
    if not phone_number or phone_number == "Unknown":
        return True
    cleaned = phone_number.replace("+", "").replace("-", "").replace(" ", "")
    if not cleaned.startswith("1") or len(cleaned) != 11:
        return True
    return False


def parse_spoken_number(text):
    """Convert spoken phone number to E.164 format."""
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
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


# ==============================================================================
# GPT HELPERS
# ==============================================================================

def gpt_extract_name(speech):
    """
    Extract clean name from messy speech.
    'Uhh yeah this is Mike sorry I am driving' -> 'Mike'
    Caches results. Falls back to rule-based if GPT unavailable.
    """
    if not speech:
        return ""

    cache_key = speech.lower().strip()
    if cache_key in gpt_name_cache:
        return gpt_name_cache[cache_key]

    if not openai_client:
        return _rule_extract_name(speech)

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=10,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a receptionist. Extract ONLY the person's name. "
                        "Strip all filler words. Return just the clean name, properly capitalized. "
                        "If no name found return: NONE"
                    )
                },
                {"role": "user", "content": speech}
            ]
        )
        name = resp.choices[0].message.content.strip()
        if name == "NONE" or len(name) < 2 or len(name) > 50:
            result = _rule_extract_name(speech)
        else:
            result = name.replace(".", "").replace(",", "").strip()
        gpt_name_cache[cache_key] = result
        return result
    except Exception as e:
        print("GPT name error: " + str(e))
        return _rule_extract_name(speech)


def _rule_extract_name(value):
    """Rule-based name extraction fallback."""
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


def gpt_extract_service_and_score(speech):
    """
    Identify service type AND lead score in one GPT call.
    Returns (service, score) tuple.
    Score: HIGH ($500+), MEDIUM ($150-500), LOW (under $150), EMERGENCY
    Caches results.
    """
    if not speech:
        return clean_text(speech), "MEDIUM"

    cache_key = speech.lower().strip()
    if cache_key in gpt_service_cache:
        return gpt_service_cache[cache_key]

    if not openai_client:
        return clean_text(speech), "MEDIUM"

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=15,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Return exactly: SERVICE|SCORE "
                        "SERVICE: Plumbing, HVAC, Electrical, Roofing, Landscaping, "
                        "Painting, Flooring, Handyman, EMERGENCY, or NONE "
                        "SCORE: HIGH (over $500), MEDIUM ($150-500), LOW (under $150) "
                        "EMERGENCY always gets HIGH. "
                        "Examples: "
                        "whole house AC replacement -> HVAC|HIGH "
                        "leaky faucet -> Plumbing|LOW "
                        "gas smell -> EMERGENCY|HIGH "
                        "AC not cooling -> HVAC|MEDIUM "
                        "new roof -> Roofing|HIGH "
                        "outlet not working -> Electrical|MEDIUM"
                    )
                },
                {"role": "user", "content": speech}
            ]
        )
        raw = resp.choices[0].message.content.strip()
        parts = raw.split("|")
        service = parts[0].strip() if len(parts) >= 1 else clean_text(speech)
        score   = parts[1].strip() if len(parts) >= 2 else "MEDIUM"
        if score not in ["HIGH", "MEDIUM", "LOW"]:
            score = "MEDIUM"
        if service in ["NONE", ""]:
            service = clean_text(speech)
        result = (service, score)
        gpt_service_cache[cache_key] = result
        print("Service: " + service + " | Score: " + score)
        return result
    except Exception as e:
        print("GPT service error: " + str(e))
        return clean_text(speech), "MEDIUM"


def gpt_build_lead_summary(name, service, intent, urgency, details, caller, score):
    """
    Build elite dollar-focused lead summary.
    HIGH VALUE: John needs full water heater replacement (~$2800). Call him back first.
    """
    if not openai_client:
        return None
    try:
        prompt = (
            "Name: " + name + "\n"
            "Service: " + service + "\n"
            "Issue: " + intent + "\n"
            "Urgency: " + urgency + "\n"
            "Details: " + details
        )
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=100,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a business advisor for a home service company. "
                        "Summarize this lead in 2 sentences MAX. "
                        "Always include the job description and a realistic dollar estimate. "
                        "Start with the value tier: "
                        "HIGH VALUE (over $1000), MID RANGE ($300-$1000), QUICK JOB (under $300). "
                        "End with a one-line action for the owner. "
                        "Example: HIGH VALUE: John needs a full water heater replacement (~$2800). "
                        "Call him back first, do not let this go to voicemail. "
                        "No greetings. No sign-offs. Be direct."
                    )
                },
                {"role": "user", "content": prompt}
            ]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("GPT summary error: " + str(e))
        return None


# ==============================================================================
# SMS
# ==============================================================================

def send_sms(to_number, body):
    try:
        if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
            print("Twilio credentials missing")
            return False
        if not to_number or to_number == "Unknown":
            return False
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=body, from_=TWILIO_PHONE_NUMBER, to=to_number)
        print("SMS sent to " + to_number)
        return True
    except Exception as e:
        print("SMS failed: " + str(e))
        return False


def send_booking_sms(to_number, name, service):
    body = (
        "Hi " + name + ", thanks for calling!\n\n"
        "To book your " + service + " appointment:\n"
        + CALENDLY_LINK + "\n\n"
        "We look forward to helping you!"
    )
    send_sms(to_number, body)


def send_lead_alert(name, caller, service, urgency, intent, details, score):
    if not OWNER_PHONE:
        return
    summary = gpt_build_lead_summary(name, service, intent, urgency, details, caller, score)
    urgency_flag = "URGENT" if urgency == "Urgent" else "Standard"

    if summary:
        body = (
            "New Lead - NextReceptionist\n\n"
            + summary + "\n\n"
            "Name: " + name + "\n"
            "Phone: " + caller + "\n"
            "Service: " + service + "\n"
            "Urgency: " + urgency_flag + "\n\n"
            "Booking link sent to customer."
        )
    else:
        body = (
            "New Lead - NextReceptionist\n\n"
            "[" + urgency_flag + "]\n"
            "Name: " + name + "\n"
            "Phone: " + caller + "\n"
            "Service: " + service + "\n"
            "Issue: " + intent + "\n\n"
            "Booking link sent to customer."
        )
    send_sms(OWNER_PHONE, body)


def send_urgent_alert(name, caller, service, intent):
    if not OWNER_PHONE:
        return
    body = (
        "URGENT CALL - NextReceptionist\n\n"
        "Name: " + name + "\n"
        "Service: " + service + "\n"
        "Issue: " + intent + "\n"
        "Phone: " + caller + "\n\n"
        "Call them back ASAP!"
    )
    send_sms(OWNER_PHONE, body)


def send_emergency_sms(caller, speech):
    if not OWNER_PHONE:
        return
    body = (
        "EMERGENCY ALERT - NextReceptionist\n\n"
        "Phone: " + caller + "\n"
        "Reported: " + speech[:200] + "\n\n"
        "CALL BACK IMMEDIATELY"
    )
    send_sms(OWNER_PHONE, body)


# ==============================================================================
# EMERGENCY RESPONSE — live dial to owner
# ==============================================================================

def emergency_response(response, caller, speech):
    """
    Immediate emergency handler.
    1. Plays pre-recorded message instantly
    2. Live dials owner so caller reaches a real person
    3. SMS alert fires to owner
    """
    send_emergency_sms(caller, speech)
    say(response, (
        "I understand this is an emergency. "
        "I am connecting you with a technician right now. "
        "Please stay on the line. If this is life threatening call 911 immediately."
    ))
    if OWNER_PHONE:
        dial = Dial(action="/call_ended", method="POST", timeout=30)
        dial.number(OWNER_PHONE)
        response.append(dial)
        say(response, (
            "We were unable to reach a technician directly. "
            "Someone will call you back within minutes. "
            "If this is life threatening please call 911 immediately. Goodbye."
        ))
    response.hangup()
    return str(response)


@app.route("/call_ended", methods=["POST"])
def call_ended():
    return str(VoiceResponse())


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
            writer.writerow(["timestamp", "name", "caller_phone",
                             "service", "intent", "urgency", "details", "score"])


def append_to_csv(name, caller, service, intent, urgency, details, score):
    with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            name, caller, service, intent, urgency, details, score
        ])
    print("Call saved to CSV")


ensure_csv_exists()


# ==============================================================================
# NO-SHOW SAVER — APScheduler confirmation calls
# ==============================================================================

def make_confirmation_call(customer_phone, customer_name, appointment_time, calendly_link):
    """Make outbound day-before confirmation call."""
    try:
        if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, APP_URL]):
            print("Missing credentials for confirmation call")
            return
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        params = urlencode({
            "name": customer_name,
            "time": appointment_time,
            "phone": customer_phone,
            "calendly": calendly_link
        })
        call = client.calls.create(
            to=customer_phone,
            from_=TWILIO_PHONE_NUMBER,
            url=APP_URL + "/confirm-appointment?" + params,
            method="GET",
            timeout=30
        )
        print("Confirmation call to " + customer_phone + " SID: " + call.sid)
    except Exception as e:
        print("Confirmation call failed: " + str(e))


def schedule_confirmation_call(customer_phone, customer_name, appointment_dt, calendly_link):
    """Schedule confirmation call for 10 AM the day before via APScheduler."""
    try:
        call_time = appointment_dt - timedelta(days=1)
        call_time = call_time.replace(hour=10, minute=0, second=0, microsecond=0)
        now = datetime.now()
        if call_time <= now:
            call_time = now + timedelta(minutes=30)
        formatted_time = appointment_dt.strftime("%B %-d at %-I:%M %p")
        job_id = "confirm_" + customer_phone + "_" + str(int(appointment_dt.timestamp()))
        scheduler.add_job(
            func=make_confirmation_call,
            trigger="date",
            run_date=call_time,
            args=[customer_phone, customer_name, formatted_time, calendly_link],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=3600
        )
        print("Confirmation scheduled: " + job_id)
    except Exception as e:
        print("Schedule error: " + str(e))


# ==============================================================================
# ROUTES — INBOUND CALL FLOW
# ==============================================================================

@app.route("/", methods=["GET"])
def home():
    return "AI Receptionist is running."


@app.route("/voice", methods=["GET", "POST"])
def voice():
    """Greeting — emergency option first, then ask for name."""
    response = VoiceResponse()
    gather = gather_speech(
        "/triage",
        hints="emergency, urgent, help, flooding, gas, fire, burst, my name is, first name",
        timeout="4"
    )
    say(gather, (
        "Thank you for calling. You've reached the service desk. "
        "If this is an emergency please say emergency now. "
        "Otherwise please tell us your name and we will be happy to help you."
    ))
    response.append(gather)
    response.redirect("/voice")
    return str(response)


@app.route("/triage", methods=["POST"])
def triage():
    """
    First response scanner — checks emergency before anything else.
    Emergency fires in under 5 seconds. Normal callers flow to name.
    """
    response    = VoiceResponse()
    speech      = request.values.get("SpeechResult", "")
    caller      = request.values.get("From", "Unknown")

    # Emergency check on very first words — instant keyword scan
    if is_emergency(speech):
        return emergency_response(response, caller, speech)

    # Try to extract name from first response
    # Many callers say "Hi my name is John" right away
    name = gpt_extract_name(speech) if speech else ""

    if name:
        next_url = build_url("/get_service", name=name, caller=caller)
        gather   = gather_speech(next_url, hints=GENERAL_HINTS)
        say(gather, (
            "Thanks " + name + ". What type of service do you need today? "
            "For example plumbing, HVAC, electrical, or roofing."
        ))
        response.append(gather)
        response.redirect(next_url)
    else:
        gather = gather_speech("/get_name", hints="my name is, first name, last name", timeout="3")
        say(gather, "What is your full name please?")
        response.append(gather)
        response.redirect("/get_name")

    return str(response)


@app.route("/get_name", methods=["POST"])
def get_name():
    response = VoiceResponse()
    speech   = request.values.get("SpeechResult", "")
    caller   = request.values.get("From", "Unknown")
    retries  = int(request.args.get("retries", 0))

    if is_emergency(speech):
        return emergency_response(response, caller, speech)

    # Play filler while GPT extracts name
    play_filler(response)
    name = gpt_extract_name(speech) if speech else ""

    if not name:
        if retries >= MAX_RETRIES:
            say(response, "I am having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url("/get_name", retries=retries + 1)
        gather    = gather_speech(retry_url, hints="my name is, first name, last name", timeout="3")
        say(gather, "I didn't catch your name. Could you please say your full name?")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url("/get_service", name=name, caller=caller)
    gather   = gather_speech(next_url, hints=GENERAL_HINTS)
    say(gather, (
        "Thanks " + name + ". What type of service do you need today? "
        "For example plumbing, HVAC, electrical, or roofing."
    ))
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_service", methods=["POST"])
def get_service():
    response = VoiceResponse()
    speech   = request.values.get("SpeechResult", "")
    name     = request.args.get("name", "")
    caller   = request.args.get("caller", "Unknown")
    retries  = int(request.args.get("retries", 0))

    if is_emergency(speech):
        return emergency_response(response, caller, speech)

    # Play filler while GPT identifies service and scores lead
    play_filler(response)
    service, score = gpt_extract_service_and_score(speech) if speech else ("", "MEDIUM")

    # Short-circuit: service + panic keyword = emergency
    PANIC_COMBOS = {
        "Plumbing":   ["flood", "burst", "flooding", "gushing", "water everywhere"],
        "HVAC":       ["gas", "gas smell", "carbon monoxide", "explosion"],
        "Electrical": ["sparking", "sparks", "fire", "shock", "electrocuted"],
        "Roofing":    ["collapse", "collapsed", "caved in"],
    }
    speech_lower = speech.lower()
    if any(w in speech_lower for w in PANIC_COMBOS.get(service, [])):
        return emergency_response(response, caller, speech)

    if service == "EMERGENCY":
        return emergency_response(response, caller, speech)

    if not service:
        if retries >= MAX_RETRIES:
            say(response, "I am having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url("/get_service", name=name, caller=caller, retries=retries + 1)
        gather    = gather_speech(retry_url, hints=GENERAL_HINTS)
        say(gather, "I didn't catch that. Please tell me the type of service you need, like plumbing, HVAC, or electrical.")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    confirm_url = build_url("/confirm_service", name=name, service=service, caller=caller, score=score)
    gather      = gather_speech(confirm_url, hints="yes, no, yeah, nope, correct, wrong", timeout="3")
    say(gather, "Got it, you need " + service + ". Is that correct? Please say yes or no.")
    response.append(gather)
    response.redirect(confirm_url)
    return str(response)


@app.route("/confirm_service", methods=["POST"])
def confirm_service():
    response = VoiceResponse()
    speech   = request.values.get("SpeechResult", "")
    name     = request.args.get("name", "")
    service  = request.args.get("service", "")
    caller   = request.args.get("caller", "Unknown")
    score    = request.args.get("score", "MEDIUM")
    retries  = int(request.args.get("retries", 0))

    answer = yes_no_answer(speech)

    if answer == "yes":
        next_url = build_url("/get_intent", name=name, service=service, caller=caller, score=score)
        gather   = gather_speech(next_url, hints=GENERAL_HINTS, timeout="4")
        say(gather, "Great. Can you briefly describe what is going on and what you need help with?")
        response.append(gather)
        response.redirect(next_url)
        return str(response)

    if answer == "no":
        next_url = build_url("/get_service", name=name, caller=caller)
        gather   = gather_speech(next_url, hints=GENERAL_HINTS)
        say(gather, "No problem. What type of service do you need?")
        response.append(gather)
        response.redirect(next_url)
        return str(response)

    if retries >= MAX_RETRIES:
        say(response, "I am having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
        response.hangup()
        return str(response)

    retry_url = build_url("/confirm_service", name=name, service=service, caller=caller, score=score, retries=retries + 1)
    gather    = gather_speech(retry_url, hints="yes, no", timeout="3")
    say(gather, "Sorry, I didn't catch that. Please say yes or no.")
    response.append(gather)
    response.redirect(retry_url)
    return str(response)


@app.route("/get_intent", methods=["POST"])
def get_intent():
    response = VoiceResponse()
    speech   = request.values.get("SpeechResult", "")
    name     = request.args.get("name", "")
    service  = request.args.get("service", "")
    caller   = request.args.get("caller", "Unknown")
    score    = request.args.get("score", "MEDIUM")
    retries  = int(request.args.get("retries", 0))

    if is_emergency(speech):
        return emergency_response(response, caller, speech)

    play_filler(response)
    intent = clean_text(speech)

    if not intent:
        if retries >= MAX_RETRIES:
            say(response, "I am having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url("/get_intent", name=name, service=service, caller=caller, score=score, retries=retries + 1)
        gather    = gather_speech(retry_url, hints=GENERAL_HINTS, timeout="4")
        say(gather, "I didn't catch that. Could you briefly describe what you need help with?")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url("/get_urgency", name=name, service=service, intent=intent, caller=caller, score=score)
    gather   = gather_speech(next_url, hints="yes, no, urgent, not urgent, emergency", timeout="3")
    say(gather, "Thank you. Is this an urgent or emergency situation? Please say yes or no.")
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_urgency", methods=["POST"])
def get_urgency():
    response = VoiceResponse()
    speech   = request.values.get("SpeechResult", "")
    name     = request.args.get("name", "")
    service  = request.args.get("service", "")
    intent   = request.args.get("intent", "")
    caller   = request.args.get("caller", "Unknown")
    score    = request.args.get("score", "MEDIUM")
    retries  = int(request.args.get("retries", 0))

    answer = yes_no_answer(speech)

    if answer == "yes":
        urgency = "Urgent"
        send_urgent_alert(name, caller, service, intent)
    elif answer == "no":
        urgency = "Not Urgent"
    else:
        if retries >= MAX_RETRIES:
            say(response, "I am having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url("/get_urgency", name=name, service=service, intent=intent, caller=caller, score=score, retries=retries + 1)
        gather    = gather_speech(retry_url, hints="yes, no, urgent, not urgent", timeout="3")
        say(gather, "Sorry, please say yes if it is urgent or no if it is not.")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url("/get_details", name=name, service=service, intent=intent, urgency=urgency, caller=caller, score=score)
    gather   = gather_speech(next_url, hints=GENERAL_HINTS, timeout="4")
    say(gather, "Got it. Finally, please share any additional details you would like us to know.")
    response.append(gather)
    response.redirect(next_url)
    return str(response)


@app.route("/get_details", methods=["POST"])
def get_details():
    response = VoiceResponse()
    speech   = request.values.get("SpeechResult", "")
    name     = request.args.get("name", "")
    service  = request.args.get("service", "")
    intent   = request.args.get("intent", "")
    urgency  = request.args.get("urgency", "")
    caller   = request.args.get("caller", "Unknown")
    score    = request.args.get("score", "MEDIUM")

    details = clean_text(speech) or "No extra details provided"

    # Play filler instantly while CSV, GPT summary, and SMS all process
    play_filler(response)

    # Check for landline — ask for mobile number to send SMS
    if is_likely_landline(caller):
        next_url = build_url(
            "/get_mobile",
            name=name, caller=caller, service=service,
            intent=intent, urgency=urgency, details=details, score=score
        )
        gather = gather_speech(next_url, hints="my number is, cell, mobile", timeout="4")
        say(gather, "Thank you. One last thing. What is the best mobile number to send your booking link to? Please say your ten digit number.")
        response.append(gather)
        response.redirect(next_url)
        return str(response)

    append_to_csv(name, caller, service, intent, urgency, details, score)
    send_lead_alert(name, caller, service, urgency, intent, details, score)
    send_booking_sms(caller, name, service)

    say(response, (
        "Perfect, we have everything we need. "
        "Thank you " + name + ". I am sending a text message to your phone right now "
        "with a link to book your " + service + " appointment. "
        "Please check your messages. We look forward to helping you. Have a great day. Goodbye."
    ))
    response.hangup()
    return str(response)


@app.route("/get_mobile", methods=["POST"])
def get_mobile():
    """Collect mobile number from landline callers."""
    response = VoiceResponse()
    speech   = request.values.get("SpeechResult", "")
    name     = request.args.get("name", "")
    caller   = request.args.get("caller", "Unknown")
    service  = request.args.get("service", "")
    intent   = request.args.get("intent", "")
    urgency  = request.args.get("urgency", "")
    details  = request.args.get("details", "No extra details provided")
    score    = request.args.get("score", "MEDIUM")
    retries  = int(request.args.get("retries", 0))

    mobile = parse_spoken_number(speech)

    if not mobile:
        if retries >= MAX_RETRIES:
            append_to_csv(name, caller, service, intent, urgency, details, score)
            send_lead_alert(name, caller, service, urgency, intent, details, score)
            say(response, "I was unable to get your number. We will follow up with you soon. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url(
            "/get_mobile",
            name=name, caller=caller, service=service,
            intent=intent, urgency=urgency, details=details, score=score,
            retries=retries + 1
        )
        gather = gather_speech(retry_url, hints="my number is, cell, mobile", timeout="4")
        say(gather, "I didn't catch that number. Could you please say your ten digit mobile number?")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    append_to_csv(name, mobile, service, intent, urgency, details, score)
    send_lead_alert(name, mobile, service, urgency, intent, details, score)
    send_booking_sms(mobile, name, service)

    say(response, (
        "Perfect, thank you " + name + ". We have received your request for " + service + ". "
        "I am sending a text message to your phone right now with a link to book your appointment. "
        "Please check your messages. We look forward to helping you. Have a great day. Goodbye."
    ))
    response.hangup()
    return str(response)


# ==============================================================================
# ROUTES — NO-SHOW SAVER
# ==============================================================================

@app.route("/confirm-appointment", methods=["GET", "POST"])
def confirm_appointment():
    response         = VoiceResponse()
    name             = request.args.get("name", "there")
    appointment_time = request.args.get("time", "your appointment")
    phone            = request.args.get("phone", "")
    calendly         = request.args.get("calendly", CALENDLY_LINK)
    params = urlencode({
        "name": name, "time": appointment_time,
        "phone": phone, "calendly": calendly
    })
    gather = Gather(
        num_digits=1,
        action=APP_URL + "/confirm-response?" + params,
        method="POST",
        timeout=10
    )
    gather.say(
        "Hi " + name + ", this is your service team calling to confirm your appointment "
        "scheduled for " + appointment_time + ". "
        "Press 1 to confirm or press 2 to reschedule.",
        voice="Polly.Joanna", language="en-US"
    )
    response.append(gather)
    response.say("We did not receive a response. Please call us back. Goodbye.", voice="Polly.Joanna", language="en-US")
    response.hangup()
    return str(response)


@app.route("/confirm-response", methods=["POST"])
def confirm_response():
    response         = VoiceResponse()
    digit            = request.values.get("Digits", "")
    name             = request.args.get("name", "there")
    appointment_time = request.args.get("time", "your appointment")
    phone            = request.args.get("phone", "")
    calendly         = request.args.get("calendly", CALENDLY_LINK)

    if digit == "1":
        response.say(
            "Perfect " + name + "! Your appointment for " + appointment_time + " is confirmed. See you then. Goodbye!",
            voice="Polly.Joanna", language="en-US"
        )
        response.hangup()
        if OWNER_PHONE:
            send_sms(OWNER_PHONE,
                "Appointment Confirmed\n\n"
                "Customer: " + name + "\n"
                "Time: " + appointment_time + "\n"
                "Phone: " + phone
            )
    elif digit == "2":
        response.say(
            "No problem " + name + "! I am sending you a link to pick a new time. Goodbye!",
            voice="Polly.Joanna", language="en-US"
        )
        response.hangup()
        send_sms(phone,
            "Hi " + name + "! No problem at all.\n\n"
            "Click below to reschedule:\n" + calendly + "\n\n"
            "We look forward to seeing you!"
        )
        if OWNER_PHONE:
            send_sms(OWNER_PHONE,
                "Reschedule Requested\n\n"
                "Customer: " + name + "\n"
                "Original: " + appointment_time + "\n"
                "Phone: " + phone
            )
    else:
        response.say("We did not receive a response. Please call us back. Goodbye.", voice="Polly.Joanna", language="en-US")
        response.hangup()

    return str(response)


@app.route("/calendly-webhook", methods=["POST"])
def calendly_webhook():
    """
    Triggered when customer books via Calendly.
    Set in Calendly: Integrations -> Webhooks -> invitee.created
    URL: https://your-app.onrender.com/calendly-webhook
    """
    try:
        data     = request.get_json(silent=True) or {}
        payload  = data.get("payload", {})
        event    = payload.get("event", {})
        invitee  = payload.get("invitee", {})

        customer_name  = invitee.get("name", "Customer")
        customer_phone = invitee.get("text_reminder_number", "")
        event_name     = event.get("name", "Appointment")
        start_time_raw = event.get("start_time", "")

        try:
            appointment_dt = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
            formatted_time = appointment_dt.strftime("%B %-d at %-I:%M %p")
        except Exception:
            appointment_dt = datetime.now() + timedelta(days=1)
            formatted_time = "your scheduled time"

        if OWNER_PHONE:
            body = (
                "Appointment Booked\n\n"
                "Customer: " + customer_name + "\n"
                "Service: " + event_name + "\n"
                "Time: " + formatted_time
            )
            if customer_phone:
                body += "\nPhone: " + customer_phone
            body += "\n\nConfirmation call scheduled for day before."
            send_sms(OWNER_PHONE, body)

        if customer_phone:
            schedule_confirmation_call(customer_phone, customer_name, appointment_dt, CALENDLY_LINK)

        return {"status": "ok"}, 200

    except Exception as e:
        print("Calendly webhook error: " + str(e))
        return {"status": "error"}, 500


# ==============================================================================
# ROUTES — CSV DASHBOARD
# ==============================================================================

@app.route("/check-csv", methods=["GET"])
def check_csv():
    token    = request.args.get("token", "")
    expected = os.environ.get("DASHBOARD_TOKEN", "")
    if expected and token != expected:
        return "Unauthorized", 401
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return "<pre>" + f.read() + "</pre>"
    return "CSV not found.", 404


@app.route("/download-csv", methods=["GET"])
def download_csv():
    token    = request.args.get("token", "")
    expected = os.environ.get("DASHBOARD_TOKEN", "")
    if expected and token != expected:
        return "Unauthorized", 401
    if os.path.exists(DATA_FILE):
        return send_file(DATA_FILE, as_attachment=True)
    return "CSV not found.", 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
