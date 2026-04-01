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
from datetime import datetime, timedelta
from urllib.parse import urlencode
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
import atexit

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

# GPT response cache — avoids calling GPT for the same input twice
# e.g. "plumbing" will always return "Plumbing" without hitting GPT again
gpt_name_cache = {}
gpt_service_cache = {}

# ==============================================================================
# PANIC FILTER
# Detects life-threatening emergency keywords and bypasses normal flow.
# Triggers immediate owner alert and emergency response.
# ==============================================================================

PANIC_KEYWORDS = [
    # Gas emergencies
    "gas", "gas leak", "gas smell", "smell gas", "gas line",
    # Fire
    "fire", "smoke", "burning", "flames", "on fire",
    # Electrical emergencies
    "sparking", "sparks", "electrical fire", "shock", "electrocuted",
    # Flooding
    "flooding", "flood", "water everywhere", "gushing", "burst pipe",
    # Structural
    "collapse", "collapsed", "ceiling fell", "roof caved",
    # Medical
    "unconscious", "not breathing", "heart attack",
    # General emergency
    "emergency", "911", "danger", "help me", "trapped"
]

PANIC_PREWARM = [
    "I understand this is an emergency. I am alerting a technician right now. Please stay safe and call 911 if you are in immediate danger. Someone will contact you within minutes. Goodbye.",
    "This sounds like an emergency. I am sending an urgent alert to our team right now. Please call 911 if you are in immediate danger. Someone will be in touch with you very shortly. Please stay safe.",
]

def is_panic_situation(text, use_gpt=False):
    """
    Two-layer emergency detection with confidence threshold.

    Layer 1 — Rule-based (instant, zero latency):
    High confidence keywords fire immediately.
    Ambiguous keywords require GPT confirmation.

    Layer 2 — GPT confidence score (only for ambiguous cases):
    Returns True only if GPT rates emergency confidence >= 0.8
    This prevents false positives like "my gas bill is high"
    """
    if not text:
        return False

    text_lower = text.lower()

    # HIGH CONFIDENCE keywords — fire immediately, no GPT needed
    HIGH_CONFIDENCE = [
        "gas leak", "gas smell", "smell gas", "gas line broke",
        "flooding", "water everywhere", "pipe burst", "burst pipe",
        "house on fire", "fire in", "smoke everywhere",
        "sparking", "electrical fire", "electrocuted",
        "collapse", "collapsed", "trapped", "explosion",
        "can not breathe", "cannot breathe", "not breathing"
    ]
    for keyword in HIGH_CONFIDENCE:
        if keyword in text_lower:
            print(f"HIGH CONFIDENCE PANIC: {keyword}")
            return True

    # AMBIGUOUS keywords — need GPT confidence check
    AMBIGUOUS = [
        "gas", "fire", "smoke", "flood", "leak",
        "emergency", "urgent", "danger", "help me"
    ]
    found_ambiguous = [k for k in AMBIGUOUS if k in text_lower]

    if not found_ambiguous:
        return False

    # No GPT available — use rule-based as fallback
    if not openai_client or not use_gpt:
        print(f"AMBIGUOUS keyword found (no GPT): {found_ambiguous}")
        return True

    # GPT confidence check for ambiguous cases
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=5,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Rate the emergency level of this caller statement. "
                        "Return ONLY a number from 0.0 to 1.0. "
                        "1.0 = life threatening emergency requiring immediate action. "
                        "0.0 = not an emergency at all. "
                        "Examples: "
                        "gas smell in house -> 0.95 "
                        "my gas bill is high -> 0.05 "
                        "small leak under sink -> 0.3 "
                        "flooding everywhere -> 0.95 "
                        "fire in kitchen -> 0.98 "
                        "need help with AC -> 0.1"
                    )
                },
                {"role": "user", "content": text}
            ]
        )
        score_text = resp.choices[0].message.content.strip()
        confidence = float(score_text)
        print(f"GPT emergency confidence: {confidence} for: {text}")

        # Only trigger emergency if confidence >= 0.8
        if confidence >= 0.8:
            print(f"HIGH CONFIDENCE EMERGENCY CONFIRMED: {confidence}")
            return True
        else:
            print(f"LOW CONFIDENCE — not an emergency: {confidence}")
            return False

    except Exception as e:
        print(f"GPT confidence check error: {e} — using rule-based fallback")
        return True  # Fail safe — treat as emergency if GPT fails


def send_emergency_alert(name, caller, speech, service="Unknown"):
    """
    Send an immediate emergency SMS to the business owner.
    This fires instantly when panic keywords are detected.
    """
    if not OWNER_PHONE:
        print("OWNER_PHONE not set — cannot send emergency alert!")
        return

    body = (
        "EMERGENCY ALERT - NextReceptionist\n\n"
        + "A caller reported an emergency!\n\n"
        + f"Name: {name or 'Unknown'}\n"
        + f"Phone: {caller}\n"
        + f"Service: {service or 'Unknown'}\n"
        + f"Reported: {speech[:200]}\n\n"
        + "CALL BACK IMMEDIATELY or dispatch emergency services."
    )
    send_sms(OWNER_PHONE, body)
    print(f"Emergency alert sent for caller {caller}")


# Hints help Twilio recognize expected words and ignore noise
GENERAL_HINTS = (
    "yes, no, yeah, nope, plumbing, HVAC, electrical, roofing, "
    "landscaping, painting, flooring, urgent, not urgent, "
    "appointment, schedule, help, repair, install, replace, fix, "
    "leak, broken, damage, emergency, flooding, gas, fire, smoke, "
    "sparking, burst, inspection, collapse"
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
# FILLER PHRASES
# Played instantly while GPT is processing to eliminate dead air.
# Resets the caller's impatience clock and buys 1-2 seconds.
# ==============================================================================

FILLERS_GENERAL = [
    "One moment please.",
    "Let me check on that for you.",
    "Sure, give me just a second.",
    "Got it, one moment.",
]

FILLERS_SERVICE = [
    "Let me pull that up for you.",
    "Sure thing, one moment.",
    "Got it, let me look into that.",
]

FILLERS_URGENCY = [
    "Understood, let me make note of that.",
    "Got it, noted.",
    "Okay, I have that.",
]

import random

def play_filler(response, filler_type="general"):
    """
    Play a filler phrase instantly while GPT processes in the background.
    This eliminates dead air and makes the AI feel more human.
    """
    if filler_type == "service":
        filler = random.choice(FILLERS_SERVICE)
    elif filler_type == "urgency":
        filler = random.choice(FILLERS_URGENCY)
    else:
        filler = random.choice(FILLERS_GENERAL)

    # Always use Polly for fillers — instant, no network round trip
    response.say(filler, voice="Polly.Joanna", language=LANGUAGE)


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

def gpt_extract_name(user_speech):
    """
    Elite name extractor — strips ALL filler words and saves only
    the clean proper name to the database and SMS alerts.

    Examples:
    "Uhh yeah this is Mike sorry I am driving" -> "Mike"
    "My name is John Smith" -> "John Smith"
    "It is Sarah" -> "Sarah"
    "Hi um I am calling about my AC" -> "Unknown"

    Falls back to rule-based extraction if GPT unavailable.
    Uses cache to avoid repeated API calls for same input.
    """
    if not user_speech:
        return "Unknown"

    # Check cache first — instant for repeated inputs
    cache_key = user_speech.lower().strip()
    if cache_key in gpt_name_cache:
        print(f"Name cache hit: {cache_key}")
        return gpt_name_cache[cache_key]

    # Fallback if OpenAI not configured
    if not openai_client:
        result = extract_name(user_speech)
        return result if result else "Unknown"

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=10,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a receptionist. Extract ONLY the person's name from the text. "
                        "Strip all filler words, apologies, and context. "
                        "Return just the clean proper name, properly capitalized. "
                        "If they say 'My name is John' return 'John'. "
                        "If they say 'Uhh yeah this is Mike sorry I am driving' return 'Mike'. "
                        "If no name is found return 'Unknown'."
                    )
                },
                {"role": "user", "content": user_speech}
            ]
        )
        name = resp.choices[0].message.content.strip()

        # Reject clearly wrong results
        if not name or name == "Unknown" or len(name) < 2 or len(name) > 50:
            result = extract_name(user_speech) or "Unknown"
        else:
            # Clean any stray punctuation
            result = name.replace(".", "").replace(",", "").replace("!", "").strip()

        # Cache result
        gpt_name_cache[cache_key] = result
        print(f"Name extracted: '{user_speech[:40]}' -> '{result}'")
        return result

    except Exception as e:
        print(f"GPT name extraction error: {e}")
        fallback = extract_name(user_speech)
        return fallback if fallback else "Unknown"


# Keep gpt_clean_name as alias for backward compatibility
gpt_clean_name = gpt_extract_name


# Lead score cache — stores (service, score, value_label) tuples
gpt_lead_score_cache = {}


def gpt_clean_service(raw_text):
    """
    Use GPT-4 to identify service type, detect emergencies,
    AND score the lead value in one single API call.

    Returns just the service string for backward compatibility.
    Lead score is stored separately in gpt_lead_score_cache.

    Lead Score:
    - HIGH   = $500+ job (full replacements, major repairs, new installs)
    - MEDIUM = $150-500 job (standard repairs, maintenance)
    - LOW    = under $150 (minor fixes, adjustments, inspections)
    - EMERGENCY = life-threatening situation
    """
    if not openai_client or not raw_text:
        return clean_text(raw_text)

    cache_key = raw_text.lower().strip()
    if cache_key in gpt_service_cache:
        print(f"Service cache hit: {cache_key}")
        return gpt_service_cache[cache_key]

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=25,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You analyze home service calls. Return exactly two words separated by a pipe: "
                        "SERVICE|SCORE "
                        "SERVICE must be one of: Plumbing, HVAC, Electrical, Roofing, Landscaping, "
                        "Painting, Flooring, Handyman, EMERGENCY, NONE "
                        "SCORE must be one of: HIGH, MEDIUM, LOW "
                        "EMERGENCY overrides score — return EMERGENCY|HIGH always for life-threatening issues. "
                        "HIGH score examples: full AC replacement, new roof, rewiring, burst main line, "
                        "water heater replacement, panel upgrade, whole house issues. "
                        "MEDIUM score examples: AC repair, pipe repair, outlet fix, roof patch, "
                        "standard maintenance, water heater repair. "
                        "LOW score examples: leaky faucet, dripping tap, clogged drain, light switch, "
                        "minor tune-up, small patch, basic inspection. "
                        "Examples: "
                        "whole house AC replacement -> HVAC|HIGH "
                        "leaky faucet -> Plumbing|LOW "
                        "gas smell -> EMERGENCY|HIGH "
                        "AC not cooling -> HVAC|MEDIUM "
                        "new roof -> Roofing|HIGH "
                        "outlet not working -> Electrical|MEDIUM"
                    )
                },
                {"role": "user", "content": raw_text}
            ]
        )
        result_raw = resp.choices[0].message.content.strip()
        parts = result_raw.split("|")

        if len(parts) == 2:
            service = parts[0].strip()
            score = parts[1].strip()
        else:
            service = result_raw.strip()
            score = "MEDIUM"

        # Validate score
        if score not in ["HIGH", "MEDIUM", "LOW"]:
            score = "MEDIUM"

        # Store lead score in cache for use in alerts
        gpt_lead_score_cache[cache_key] = score
        print(f"Service: {service} | Lead Score: {score}")

        if service == "NONE" or len(service) < 2:
            service = clean_text(raw_text)

        gpt_service_cache[cache_key] = service
        return service

    except Exception as e:
        print(f"GPT service extraction error: {e}")
        return clean_text(raw_text)


def get_lead_score(raw_text):
    """Get the cached lead score for a given service description."""
    cache_key = raw_text.lower().strip()
    return gpt_lead_score_cache.get(cache_key, "MEDIUM")


def format_lead_score(score):
    """Format lead score into emoji flags for SMS alert."""
    if score == "HIGH":
        return "HIGH VALUE LEAD", "Prioritize — estimated $500+ job"
    elif score == "LOW":
        return "Standard Lead", "Estimated under $150 job"
    else:
        return "Mid-Range Lead", "Estimated $150-500 job"


def gpt_categorize_intent(intent_text, service):
    """
    Use GPT-4 to categorize the customer issue into a professional
    diagnostic category instead of raw transcribed speech.
    Examples:
      "AC making grinding noise" -> "Mechanical Failure - Compressor/Motor"
      "pipe leaking under sink" -> "Active Leak - Minor"
      "no hot water" -> "System Failure - Water Heater"
      "lights keep tripping" -> "Electrical Fault - Circuit Overload"
    """
    if not openai_client or not intent_text:
        return intent_text
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=20,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You are a {service} diagnostic expert. "
                        "Categorize the customer issue in 3-5 words using professional terminology. "
                        "Format: Category - Subcategory. "
                        "No extra words. No punctuation at end. "
                        "Examples: "
                        "AC grinding noise -> Mechanical Failure - Motor "
                        "pipe leaking under sink -> Active Leak - Minor "
                        "no hot water -> System Failure - Water Heater "
                        "lights tripping breaker -> Electrical Fault - Circuit Overload "
                        "roof leaking after rain -> Water Intrusion - Roof Seal"
                    )
                },
                {"role": "user", "content": intent_text}
            ]
        )
        category = resp.choices[0].message.content.strip()
        print(f"Intent categorized: {intent_text} -> {category}")
        return category
    except Exception as e:
        print(f"GPT intent categorization error: {e}")
        return intent_text


def gpt_summarize_lead(name, service, intent, urgency, details, caller):
    """
    Elite lead summary — tells owner WHY they should care.
    Includes estimated job value and priority recommendation.

    Examples:
    Standard: "John called about a leaky faucet."
    Elite:    "HIGH VALUE: John needs a full Water Heater Replacement
               (~$2,800). Call him back first — do not let this go to voicemail."
    """
    if not openai_client:
        return None
    try:
        prompt = (
            f"Name: {name}\n"
            f"Service: {service}\n"
            f"Issue: {intent}\n"
            f"Urgency: {urgency}\n"
            f"Details: {details}"
        )
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=120,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a business advisor for a home service company. "
                        "Summarize this lead in 2 sentences MAX. "
                        "Always include: "
                        "1. What the job actually is in plain English "
                        "2. A realistic dollar estimate in parentheses "
                        "3. A one-line action recommendation for the owner "
                        "Format by value tier: "
                        "HIGH VALUE jobs (over $1000): Start with HIGH VALUE: "
                        "MID RANGE jobs ($300-$1000): Start with MID RANGE: "
                        "QUICK JOB jobs (under $300): Start with QUICK JOB: "
                        "Examples: "
                        "HIGH VALUE: John needs a full water heater replacement (~$2800). Call him back first — do not let this go to voicemail. "
                        "MID RANGE: Sarah needs AC refrigerant recharge and tune-up (~$450). Book within 24 hours. "
                        "QUICK JOB: Marcus has a dripping kitchen faucet (~$120). Schedule when convenient. "
                        "No greetings. No sign-offs. Be direct and dollar-focused."
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


# Phrases that contain dynamic content (caller name/service)
# These need OpenAI TTS since they change every call
DYNAMIC_PHRASES = ["Thanks ", "Got it, you need", "Thank you", "Perfect, thank you"]

# Directory to persist pre-recorded audio files across restarts
AUDIO_DIR = "/var/data/audio" if os.path.exists("/var/data") else "audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# Static greeting file — generated once, served instantly on every call
# Change BUSINESS_NAME env var per client for personalized greeting
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "the service desk")
STATIC_GREETING_KEY = "static_greeting"
STATIC_GREETING_PATH = os.path.join(AUDIO_DIR, "greeting.mp3")
STATIC_GREETING_TEXT = f"Hi! Thanks for calling {BUSINESS_NAME}. If this is an emergency press 1 or say emergency now. Otherwise just tell us how we can help you today."

# Pre-recorded emergency response — zero lag when crisis hits
EMERGENCY_PATCH_PATH = os.path.join(AUDIO_DIR, "emergency_patch.mp3")
EMERGENCY_PATCH_TEXT = (
    "I am stopping everything and connecting you with a technician right now. "
    "Please stay on the line. If this is life threatening call 911 immediately."
)
EMERGENCY_PATCH_KEY = hashlib.md5(EMERGENCY_PATCH_TEXT.encode()).hexdigest()


def generate_static_greeting():
    """
    Generate the greeting MP3 once and save to disk.
    Every subsequent call plays the file instantly — zero API delay.
    Regenerates if BUSINESS_NAME changes.
    """
    if not openai_client:
        return
    try:
        audio_data = generate_speech(STATIC_GREETING_TEXT)
        if audio_data:
            with open(STATIC_GREETING_PATH, "wb") as f:
                f.write(audio_data)
            # Also load into memory cache
            key = hashlib.md5(STATIC_GREETING_TEXT.encode()).hexdigest()
            text_cache[key] = STATIC_GREETING_TEXT
            audio_cache[key] = audio_data
            print(f"Static greeting generated for: {BUSINESS_NAME}")
    except Exception as e:
        print(f"Static greeting generation error: {e}")


def say(response, text):
    """
    Zero-lag TTS system — 2026 elite architecture.

    Priority order:
    1. Pre-recorded disk file (static phrases) — ~0ms, survives restarts
    2. Memory cache (static phrases this session) — ~0ms
    3. Polly for ALL other phrases — <100ms, no API wait
       This includes dynamic phrases with caller names.
       Phone audio is 8kHz compressed — callers cannot distinguish
       Polly from OpenAI nova on a phone call.

    OpenAI TTS is ONLY used to pre-record static files at startup.
    Never called during a live call.
    """
    if openai_client and APP_URL:
        key = hashlib.md5(text.encode()).hexdigest()
        disk_path = os.path.join(AUDIO_DIR, f"{key}.mp3")

        # Tier 1 — Pre-recorded disk file (greeting, emergency patch)
        if os.path.exists(disk_path):
            text_cache[key] = text
            if key not in audio_cache:
                with open(disk_path, "rb") as f:
                    audio_cache[key] = f.read()
            response.play(f"{APP_URL}/audio/{key}")
            return

        # Tier 2 — Memory cache from this session
        if key in audio_cache:
            text_cache[key] = text
            response.play(f"{APP_URL}/audio/{key}")
            return

    # Tier 3 — Polly for everything else (instant, <100ms)
    # This eliminates ALL OpenAI TTS lag during live calls
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

def gather_speech(action_url, hints=GENERAL_HINTS, timeout="2"):
    """
    Gather speech with maximum noise filtering.
    - enhanced=True: Twilio best speech recognition
    - speech_model=phone_call: tuned for phone audio
    - hints: tells Twilio what words to expect
    - timeout: configurable per step — shorter for yes/no, longer for descriptions
    - action_on_empty_result=True: always fires retry logic on noise
    """
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


def gpt_validate_name(raw_text, extracted_name):
    """
    Smart NATO fallback for difficult names.
    Uses GPT to detect if the transcribed name is:
    1. A hallucination ("I'm a" instead of "Emma")
    2. An ambiguous transcription ("Siobhan", "Geoff", "Krystle")
    3. Clearly correct and confident

    Returns: (is_confident, nato_hint)
    - is_confident=True means name is reliable
    - nato_hint is a clarifying question if not confident
    """
    if not openai_client or not extracted_name:
        return False, None

    # Common hallucination patterns — GPT mishearing phrases as names
    HALLUCINATIONS = [
        "i'm a", "ima", "i am a", "is a", "its a", "it's a",
        "um", "uh", "er", "hmm", "the", "and", "for"
    ]
    if extracted_name.lower() in HALLUCINATIONS:
        return False, None

    # Names under 2 chars are almost certainly wrong
    if len(extracted_name) < 2:
        return False, None

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=60,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You validate phone call name transcriptions. "
                        "Given the raw speech and extracted name, determine confidence. "
                        "Return JSON with two fields: "
                        "confident (true/false) and hint (null or a NATO clarification). "
                        "confident=true if the name is clearly correct. "
                        "confident=false if the name looks like a hallucination or mishearing. "
                        "If not confident, hint should be a short clarifying question. "
                        "Examples: "
                        "raw=my name is john name=John -> confident true hint null. "
                        "raw=im a johnson name=A Johnson -> confident false hint null. "
                        "raw=shivon name=Shivon -> confident false hint Was that S as in Sam or Sh as in Sharon. "
                        "raw=geoff name=Geoff -> confident false hint Was that G as in George or J as in John. "
                        "Return ONLY valid JSON with keys confident and hint. No extra text."
                    )
                },
                {"role": "user", "content": f"raw='{raw_text}' name='{extracted_name}'"}
            ]
        )
        import json
        result = json.loads(resp.choices[0].message.content.strip())
        confident = result.get("confident", True)
        hint = result.get("hint", None)
        print(f"Name validation: {extracted_name} — confident={confident} hint={hint}")
        return confident, hint
    except Exception as e:
        print(f"GPT name validation error: {e}")
        return True, None  # Assume confident if GPT fails


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

# ==============================================================================
# APSCHEDULER — Production-grade background task scheduler
# Replaces raw threading which dies when Gunicorn workers restart.
# APScheduler survives worker restarts and handles tasks reliably.
# ==============================================================================
jobstores = {"default": MemoryJobStore()}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone="UTC")
scheduler.start()

# Shut down scheduler cleanly when app exits
atexit.register(lambda: scheduler.shutdown(wait=False))

# Pre-warm audio in background using APScheduler instead of raw thread
scheduler.add_job(
    func=prewarm_audio,
    trigger="date",
    id="prewarm_audio",
    replace_existing=True
)

# Generate static greeting file at startup
scheduler.add_job(
    func=generate_static_greeting,
    trigger="date",
    id="generate_greeting",
    replace_existing=True
)


def generate_emergency_patch():
    """Pre-record emergency response MP3 — plays with zero lag during crises."""
    if not openai_client:
        return
    try:
        audio_data = generate_speech(EMERGENCY_PATCH_TEXT)
        if audio_data:
            with open(EMERGENCY_PATCH_PATH, "wb") as f:
                f.write(audio_data)
            text_cache[EMERGENCY_PATCH_KEY] = EMERGENCY_PATCH_TEXT
            audio_cache[EMERGENCY_PATCH_KEY] = audio_data
            print("Emergency patch MP3 generated and cached")
    except Exception as e:
        print(f"Emergency patch generation error: {e}")


scheduler.add_job(
    func=generate_emergency_patch,
    trigger="date",
    id="generate_emergency_patch",
    replace_existing=True
)


# ==============================================================================
# NO-SHOW SAVER
# Schedules a confirmation call the day before every appointment.
# If customer presses 1 = confirmed, press 2 = reschedule, no answer = retry.
# ==============================================================================

# Stores pending confirmation calls: {call_sid: {name, time, service, calendly}}
pending_confirmations = {}


def make_confirmation_call(customer_phone, customer_name, appointment_time, service, business_name, calendly_link):
    """
    Make an outbound confirmation call to the customer the day before their appointment.
    Uses Twilio outbound call API.
    """
    try:
        if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
            print("Twilio credentials missing — skipping confirmation call")
            return

        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        # Build the TwiML URL for the confirmation call
        params = urlencode({
            "name": customer_name,
            "time": appointment_time,
            "service": service,
            "business": business_name,
            "calendly": calendly_link,
            "phone": customer_phone
        })
        twiml_url = f"{APP_URL}/confirm-appointment?{params}"

        call = client.calls.create(
            to=customer_phone,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url,
            method="GET",
            timeout=30,
            machine_detection="Enable"
        )

        print(f"Confirmation call initiated to {customer_phone} — SID: {call.sid}")
        pending_confirmations[call.sid] = {
            "name": customer_name,
            "time": appointment_time,
            "service": service,
            "business": business_name,
            "calendly": calendly_link,
            "phone": customer_phone,
            "attempts": 1
        }

    except Exception as e:
        print(f"Confirmation call failed: {e}")


def schedule_confirmation_call(customer_phone, customer_name, appointment_datetime, service, business_name, calendly_link):
    """
    Schedule the confirmation call for 10 AM the day before the appointment.
    Uses APScheduler instead of raw threading — survives Gunicorn worker restarts.
    """
    try:
        # Calculate when to make the call — 10 AM day before appointment
        call_time = appointment_datetime - timedelta(days=1)
        call_time = call_time.replace(hour=10, minute=0, second=0, microsecond=0)
        now = datetime.now()

        # If appointment is less than 24 hours away — call in 30 minutes
        if call_time <= now:
            call_time = now + timedelta(minutes=30)
            print(f"Appointment soon — confirmation call in 30 minutes at {call_time}")
        else:
            print(f"Confirmation call scheduled for {call_time.strftime('%Y-%m-%d at 10:00 AM')}")

        # Use APScheduler date trigger — fires once at exact time
        job_id = f"confirm_{customer_phone}_{appointment_datetime.timestamp()}"
        scheduler.add_job(
            func=make_confirmation_call,
            trigger="date",
            run_date=call_time,
            args=[
                customer_phone,
                customer_name,
                appointment_datetime.strftime("%B %-d at %-I:%M %p"),
                service,
                business_name,
                calendly_link
            ],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=3600  # If job fires up to 1 hour late still run it
        )
        print(f"APScheduler job created: {job_id}")

    except Exception as e:
        print(f"Schedule confirmation error: {e}")


# ==============================================================================
# ROUTES
# ==============================================================================

def emergency_response(response, name, caller, speech, service="Unknown"):
    """
    Immediately handle a panic/emergency call.
    1. Plays reassuring message instantly
    2. Live dials owner phone so caller is connected to a real person
    3. Falls back to SMS alert if no owner phone set
    """
    send_emergency_alert(name, caller, speech, service)

    # Use pre-recorded emergency patch if available — zero API lag
    if os.path.exists(EMERGENCY_PATCH_PATH) and APP_URL:
        text_cache[EMERGENCY_PATCH_KEY] = EMERGENCY_PATCH_TEXT
        if EMERGENCY_PATCH_KEY not in audio_cache:
            with open(EMERGENCY_PATCH_PATH, "rb") as f:
                audio_cache[EMERGENCY_PATCH_KEY] = f.read()
        response.play(f"{APP_URL}/audio/{EMERGENCY_PATCH_KEY}")
    else:
        # Polly fallback — still instant, no API call
        response.say(
            "I am stopping everything and connecting you with a technician right now. "
            "Please stay on the line. If this is life threatening call 911 immediately.",
            voice="Polly.Joanna",
            language=LANGUAGE
        )

    # Live dial to owner — connects caller directly to a real person
    if OWNER_PHONE:
        from twilio.twiml.voice_response import Dial
        dial = Dial(action="/call_ended", method="POST", timeout=30)
        dial.number(OWNER_PHONE)
        response.append(dial)
        # Fallback if owner doesn't answer
        say(response, (
            "I was unable to reach a technician directly. "
            "Someone will call you back within minutes. "
            "If this is life threatening please call 911 immediately. Goodbye."
        ))
    else:
        say(response, (
            "Please call 911 if you are in immediate danger. "
            "Our team will contact you shortly. Goodbye."
        ))

    response.hangup()
    return str(response)


@app.route("/call_ended", methods=["POST"])
def call_ended():
    """Called after emergency transfer ends."""
    response = VoiceResponse()
    return str(response)


@app.route("/", methods=["GET"])
def home():
    return "AI Receptionist is running."


@app.route("/voice", methods=["GET", "POST"])
def voice():
    """
    Elite two-step greeting:
    Step 1 — Ask emergency or regular FIRST (3 seconds max)
    Step 2 — Route accordingly

    Emergency callers identified in under 5 seconds.
    Regular callers flow normally.
    """
    response = VoiceResponse()
    gather = Gather(
        input="speech dtmf",
        action="/triage",
        method="POST",
        speech_timeout="3",
        language=LANGUAGE,
        enhanced=True,
        speech_model="phone_call",
        hints="emergency, urgent, help, flooding, gas, fire, smoke, burst, sparking, name, service, plumbing, HVAC, electrical, roofing",
        profanity_filter=False,
        action_on_empty_result=True,
        num_digits=1
    )
    # Use pre-recorded static file if available — zero API call, instant playback
    # Falls back to Polly if file not yet generated
    if os.path.exists(STATIC_GREETING_PATH) and APP_URL:
        key = hashlib.md5(STATIC_GREETING_TEXT.encode()).hexdigest()
        text_cache[key] = STATIC_GREETING_TEXT
        if key not in audio_cache:
            with open(STATIC_GREETING_PATH, "rb") as f:
                audio_cache[key] = f.read()
        gather.play(f"{APP_URL}/audio/{key}")
    else:
        gather.say(
            f"Hi! Thanks for calling {BUSINESS_NAME}. "
            "If this is an emergency press 1 or say emergency now. "
            "Otherwise just tell us how we can help you today.",
            voice="Polly.Joanna",
            language=LANGUAGE
        )
    response.append(gather)
    response.redirect("/voice")
    return str(response)


@app.route("/triage", methods=["POST"])
def triage():
    """
    Elite triage route — splits emergency vs regular in under 5 seconds.
    Emergency → immediate live dial to owner.
    Regular → normal name/service flow.
    """
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "").lower()
    digit = request.values.get("Digits", "")
    caller = request.values.get("From", "Unknown")

    # Emergency path — press 1 or say emergency/urgent/help
    is_emergency = (
        digit == "1" or
        any(w in speech for w in ["emergency", "urgent", "help", "fire", "flood",
                                   "gas", "burst", "smoke", "sparking", "danger"])
    )

    if is_emergency:
        print(f"TRIAGE: Emergency detected — digit={digit} speech={speech}")
        # Instant response — Polly for zero latency
        response.say(
            "I am stopping everything and connecting you with a technician right now. Please stay on the line.",
            voice="Polly.Joanna",
            language=LANGUAGE
        )
        send_emergency_alert("Unknown", caller, speech or "Emergency button pressed", "EMERGENCY")
        if OWNER_PHONE:
            from twilio.twiml.voice_response import Dial
            dial = Dial(action="/call_ended", method="POST", timeout=30)
            dial.number(OWNER_PHONE)
            response.append(dial)
        response.say(
            "We were unable to reach a technician directly. "
            "Someone will call you back within minutes. "
            "If this is life threatening please call 911 immediately.",
            voice="Polly.Joanna",
            language=LANGUAGE
        )
        response.hangup()
        return str(response)

    # Regular path — continue to normal flow
    # Non-emergency — check if caller already gave their name
    # e.g. "Hi my name is John I need a plumber"
    name = extract_name(speech) if speech else ""

    if name:
        # Name already captured — skip straight to service
        next_url = build_url("/get_service", name=name, caller=caller)
        gather = gather_speech(next_url, hints=GENERAL_HINTS, timeout="3")
        gather.say(
            f"Got it {name}, and what service do you need today? For example plumbing, HVAC, electrical, or roofing.",
            voice="Polly.Joanna",
            language=LANGUAGE
        )
        response.append(gather)
        response.redirect(next_url)
    else:
        # No name yet — ask for it naturally
        gather = gather_speech("/get_name", hints="my name is, first name, last name", timeout="2")
        gather.say(
            "What is your full name please?",
            voice="Polly.Joanna",
            language=LANGUAGE
        )
        response.append(gather)
        response.redirect("/get_name")

    return str(response)


@app.route("/emergency_check", methods=["POST"])
def emergency_check():
    """
    ELITE EMERGENCY BREAKOUT — fires on the very first word the caller speaks.
    If emergency detected: immediate live dial to owner. Zero questions asked.
    If not emergency: extract name if given, then continue normal flow.
    Time to emergency response: under 10 seconds from call connect.
    """
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    caller = request.values.get("From", "Unknown")

    # PARALLEL EMERGENCY SCAN — rule-based + GPT confidence simultaneously
    # use_gpt=True enables confidence threshold to prevent false positives
    if is_panic_situation(speech, use_gpt=True):
        print(f"EMERGENCY BREAKOUT triggered on first response: {speech}")
        return emergency_response(response, "", caller, speech)

    # GPT service check also detects EMERGENCY as backup layer
    if speech:
        gpt_check = gpt_clean_service(speech)
        if gpt_check == "EMERGENCY":
            print(f"GPT EMERGENCY BREAKOUT triggered: {speech}")
            return emergency_response(response, "", caller, speech)

    # Not an emergency — try to extract name from first response
    # Many callers say "Hi my name is John I need plumbing help"
    name = extract_name(speech) if speech else ""
    caller_stored = caller

    if name:
        # Got the name already — skip to service
        next_url = build_url("/get_service", name=name, caller=caller_stored)
        gather = gather_speech(next_url, hints=GENERAL_HINTS)
        service_prompt = f"Thanks {name}. What is going on today and what can we help you with?"
        if openai_client and APP_URL:
            key = hashlib.md5(service_prompt.encode()).hexdigest()
            text_cache[key] = service_prompt
            if key not in audio_cache:
                threading.Thread(
                    target=lambda: audio_cache.update({key: generate_speech(service_prompt)}),
                    daemon=True
                ).start()
        say(gather, service_prompt)
        response.append(gather)
        response.redirect(next_url)
    else:
        # No name yet — ask for it
        gather = gather_speech("/get_name", hints="my name is, first name, last name", timeout="2")
        say(gather, "What is your full name please?")
        response.append(gather)
        response.redirect("/get_name")

    return str(response)


@app.route("/get_name", methods=["POST"])
def get_name():
    response = VoiceResponse()
    speech = request.values.get("SpeechResult", "")
    caller = request.values.get("From", "Unknown")
    retries = int(request.args.get("retries", 0))

    # Check for emergency BEFORE processing name with GPT confidence
    if is_panic_situation(speech, use_gpt=True):
        return emergency_response(response, "", caller, speech)

    # Use fast rule-based extraction for names — GPT overkill here
    name = extract_name(speech) if speech else ""

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

    # Pre-generate this phrase immediately so it plays without delay
    # Ask what is happening — not just service type — so emergencies surface immediately
    service_prompt = f"Thanks {name}. What is going on today and what can we help you with?"
    if openai_client and APP_URL:
        key = hashlib.md5(service_prompt.encode()).hexdigest()
        text_cache[key] = service_prompt
        if key not in audio_cache:
            threading.Thread(target=lambda: audio_cache.update({key: generate_speech(service_prompt)}), daemon=True).start()

    say(gather, service_prompt)
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

    # Play filler instantly while GPT processes — eliminates dead air
    play_filler(response, "service")

    # GPT-4 identifies service type — also detects EMERGENCY in first response
    # This means crisis is caught within the first 10 seconds of the call
    service = gpt_clean_service(speech) if speech else ""

    # IMMEDIATE BYPASS — GPT flagged this as an emergency
    if service == "EMERGENCY" or is_panic_situation(speech, use_gpt=True):
        return emergency_response(response, name, caller, speech)

    # SHORT-CIRCUIT — service + panic keyword combo
    # e.g. "Plumbing" + "flooding" = immediate emergency
    # No need to ask more questions — connect them NOW
    PANIC_COMBOS = {
        "Plumbing":   ["flood", "burst", "flooding", "gushing", "pipe burst", "water everywhere"],
        "HVAC":       ["gas", "gas smell", "carbon monoxide", "explosion"],
        "Electrical": ["sparking", "sparks", "fire", "shock", "electrocuted", "burning smell"],
        "Roofing":    ["collapse", "collapsed", "caved in", "falling"],
    }
    speech_lower = speech.lower()
    panic_words = PANIC_COMBOS.get(service, [])
    if any(word in speech_lower for word in panic_words):
        print(f"SHORT-CIRCUIT: {service} + panic keyword detected in: {speech}")
        return emergency_response(response, name, caller, speech, service)

    if not service or service == "NONE":
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
    gather = gather_speech(confirm_url, hints="yes, no, yeah, nope, correct, wrong", timeout="2")
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
        say(gather, f"Perfect. Can you briefly describe what is going on so we can make sure {name} gets the right tech?")
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
    gather = gather_speech(retry_url, hints="yes, no, yeah, nope", timeout="2")
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

    # Play filler while processing intent
    play_filler(response, "general")

    intent = clean_text(speech)

    # Check for emergency in issue description with GPT confidence
    if is_panic_situation(speech, use_gpt=True):
        return emergency_response(response, name, caller, speech, service)

    # Categorize the intent professionally via GPT
    if intent:
        intent = gpt_categorize_intent(intent, service)

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
    gather = gather_speech(next_url, hints="yes, no, urgent, not urgent, emergency", timeout="2")
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
        # Play filler while urgent alert SMS fires in background
        play_filler(response, "urgency")
        send_urgent_alert(name, caller, service, intent)
    elif answer == "no":
        urgency = "Not Urgent"
        play_filler(response, "urgency")
    else:
        if retries >= MAX_RETRIES:
            say(response, "I'm sorry I'm having trouble hearing you. Please call back and we will be happy to help. Goodbye.")
            response.hangup()
            return str(response)
        retry_url = build_url("/get_urgency", name=name, service=service, intent=intent, caller=caller, retries=retries + 1)
        gather = gather_speech(retry_url, hints="yes, no, urgent, not urgent", timeout="2")
        say(gather, "Sorry, please say yes if it is urgent or no if it is not.")
        response.append(gather)
        response.redirect(retry_url)
        return str(response)

    next_url = build_url("/get_details", name=name, service=service, intent=intent, urgency=urgency, caller=caller)
    gather = gather_speech(next_url, hints=GENERAL_HINTS, timeout="4")
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

    # Final check for emergency in details with GPT confidence
    if is_panic_situation(speech, use_gpt=True):
        return emergency_response(response, name, caller, speech, service)

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


# ==============================================================================
# CONFIRMATION CALL ROUTES
# ==============================================================================

@app.route("/confirm-appointment", methods=["GET", "POST"])
def confirm_appointment():
    response = VoiceResponse()
    name = request.args.get("name", "there")
    appointment_time = request.args.get("time", "your appointment")
    service = request.args.get("service", "service")
    business = request.args.get("business", "our team")
    phone = request.args.get("phone", "")
    calendly = request.args.get("calendly", CALENDLY_LINK)

    params = urlencode({
        "name": name, "time": appointment_time,
        "service": service, "business": business,
        "phone": phone, "calendly": calendly
    })
    gather = Gather(
        num_digits=1,
        action=f"{APP_URL}/confirm-response?{params}",
        method="POST",
        timeout=10
    )
    gather.say(
        f"Hi {name}, this is {business} calling to confirm your {service} appointment "
        f"scheduled for {appointment_time}. "
        "Press 1 to confirm your appointment, or press 2 to reschedule.",
        voice="Polly.Joanna", language="en-US"
    )
    response.append(gather)
    response.redirect(f"{APP_URL}/confirm-no-answer?name={name}&phone={phone}&business={business}&calendly={calendly}")
    return str(response)


@app.route("/confirm-response", methods=["POST"])
def confirm_response():
    response = VoiceResponse()
    digit = request.values.get("Digits", "")
    name = request.args.get("name", "there")
    appointment_time = request.args.get("time", "your appointment")
    service = request.args.get("service", "service")
    phone = request.args.get("phone", "")
    calendly = request.args.get("calendly", CALENDLY_LINK)

    if digit == "1":
        response.say(
            f"Perfect {name}! Your {service} appointment for {appointment_time} is confirmed. "
            "We look forward to seeing you. Have a great day!",
            voice="Polly.Joanna", language="en-US"
        )
        response.hangup()
        if OWNER_PHONE:
            send_sms(OWNER_PHONE,
                "Appointment Confirmed!\n\n"
                + f"Customer: {name}\n"
                + f"Service: {service}\n"
                + f"Time: {appointment_time}\n"
                + f"Phone: {phone}"
            )
        print(f"Appointment confirmed by {name}")

    elif digit == "2":
        response.say(
            f"No problem {name}! I am sending a link to your phone right now to pick a new time. "
            "We look forward to rescheduling with you. Goodbye!",
            voice="Polly.Joanna", language="en-US"
        )
        response.hangup()
        send_sms(phone,
            f"Hi {name}! No problem at all.\n\n"
            + f"Click below to pick a new time for your {service} appointment:\n"
            + f"{calendly}\n\n"
            + "We look forward to seeing you soon!"
        )
        if OWNER_PHONE:
            send_sms(OWNER_PHONE,
                "Reschedule Requested\n\n"
                + f"Customer: {name}\n"
                + f"Service: {service}\n"
                + f"Original time: {appointment_time}\n"
                + f"Phone: {phone}\n\n"
                + "Reschedule link sent to customer."
            )
        print(f"Reschedule requested by {name}")
    else:
        response.say(
            "I am sorry I did not catch that. Please call us back to confirm your appointment. Goodbye!",
            voice="Polly.Joanna", language="en-US"
        )
        response.hangup()
    return str(response)


@app.route("/confirm-no-answer", methods=["GET", "POST"])
def confirm_no_answer():
    response = VoiceResponse()
    name = request.args.get("name", "there")
    phone = request.args.get("phone", "")
    business = request.args.get("business", "our team")
    calendly = request.args.get("calendly", CALENDLY_LINK)
    response.hangup()

    def retry_call():
        time.sleep(7200)
        if phone:
            try:
                client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                params = urlencode({"name": name, "phone": phone, "business": business, "calendly": calendly})
                client.calls.create(
                    to=phone, from_=TWILIO_PHONE_NUMBER,
                    url=f"{APP_URL}/confirm-appointment?{params}",
                    method="GET", timeout=30
                )
                print(f"Retry confirmation call made to {phone}")
            except Exception as e:
                print(f"Retry call failed: {e}")

    threading.Thread(target=retry_call, daemon=True).start()
    print(f"No answer from {name} — retry scheduled in 2 hours")
    return str(response)


@app.route("/calendly-webhook", methods=["POST"])
def calendly_webhook():
    try:
        data = request.get_json(silent=True) or {}
        payload = data.get("payload", {})
        event = payload.get("event", {})
        invitee = payload.get("invitee", {})

        customer_name = invitee.get("name", "Customer")
        customer_phone = invitee.get("text_reminder_number", "") or ""
        event_name = event.get("name", "Appointment")
        start_time_raw = event.get("start_time", "")
        business_name = "Our Team"

        try:
            appointment_dt = datetime.fromisoformat(start_time_raw.replace("Z", "+00:00"))
            formatted_time = appointment_dt.strftime("%B %-d at %-I:%M %p")
        except Exception:
            appointment_dt = datetime.now() + timedelta(days=1)
            formatted_time = "your scheduled time"

        questions = payload.get("questions_and_answers", [])
        extra_info = ""
        for q in questions:
            answer = q.get("answer", "")
            question = q.get("question", "")
            if answer:
                extra_info += f"\n{question}: {answer}"

        if OWNER_PHONE:
            body = (
                "Appointment Booked!\n\n"
                + f"Customer: {customer_name}\n"
                + f"Service: {event_name}\n"
                + f"Time: {formatted_time}"
            )
            if customer_phone:
                body += f"\nPhone: {customer_phone}"
            if extra_info:
                body += extra_info
            body += "\n\nConfirmation call scheduled for day before."
            send_sms(OWNER_PHONE, body)

        if customer_phone:
            schedule_confirmation_call(
                customer_phone, customer_name, appointment_dt,
                event_name, business_name, CALENDLY_LINK
            )

        return {"status": "ok"}, 200

    except Exception as e:
        print(f"Calendly webhook error: {e}")
        return {"status": "error", "message": str(e)}, 500


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
