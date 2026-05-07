"""
AI Receptionist — ElevenLabs Conversational AI Agent Edition
============================================================
Architecture:
  - ElevenLabs Agent handles 100% of the call conversation (STT + LLM + TTS)
  - Twilio routes inbound calls to ElevenLabs automatically (configured in ElevenLabs dashboard)
  - This Flask app provides two hooks ElevenLabs calls:
      1. /el-webhook       — called at call start, returns caller data to personalize the agent
      2. /post-call-webhook — called after call ends with full transcript + extracted data
  - Everything else (CSV, SMS, Calendly, confirmation calls) stays the same

Setup Steps:
  1. Go to elevenlabs.io → Agents → Create Agent
  2. Paste the AGENT_SYSTEM_PROMPT below as your agent's system prompt
  3. Add dynamic variables: caller_phone, business_name, calendly_link, owner_phone
  4. Go to Telephony → Phone Numbers → Import from Twilio
  5. Enter your Twilio credentials — ElevenLabs auto-configures the webhook
  6. In Agent Security settings: enable "Fetch conversation initiation data" 
     and set webhook URL to: https://your-app.onrender.com/el-webhook
  7. In ElevenLabs workspace Settings: set post-call webhook to:
     https://your-app.onrender.com/post-call-webhook
  8. Deploy this Flask app — it only needs to handle the two webhooks above

Environment Variables:
  OPENAI_API_KEY        — for GPT lead scoring and summaries
  TWILIO_ACCOUNT_SID    — for outbound SMS
  TWILIO_AUTH_TOKEN     — for outbound SMS
  TWILIO_PHONE_NUMBER   — for outbound SMS
  ELEVENLABS_API_KEY    — for ElevenLabs agent API calls
  ELEVENLABS_AGENT_ID   — your agent ID from the ElevenLabs dashboard
  ELEVENLABS_WEBHOOK_SECRET — secret to verify post-call webhooks
  CALENDLY_LINK         — your booking link
  APP_URL               — your deployed app URL
  OWNER_PHONE           — owner's phone for lead alerts
  DASHBOARD_TOKEN       — for /check-csv and /download-csv auth
"""

# ==============================================================================
# AGENT_SYSTEM_PROMPT — paste this into your ElevenLabs agent's system prompt
# ==============================================================================
AGENT_SYSTEM_PROMPT = """
You are a professional AI receptionist for a home services company. 
Your job is to warmly greet callers, collect their information, and help them book an appointment.

The caller's phone number is: {{caller_phone}}
The business name is: {{business_name}}
The booking link is: {{calendly_link}}

CONVERSATION FLOW:
1. Greet the caller warmly. Say: "Thank you for calling {{business_name}}! If this is an emergency, please say emergency now. Otherwise, what is your name?"
2. Get their FULL NAME. Confirm it back to them.
3. Ask what SERVICE they need. Options: Plumbing, HVAC, Electrical, Roofing, Landscaping, Painting, Flooring, Handyman.
4. Ask them to briefly DESCRIBE the issue.
5. Ask if it is URGENT or not.
6. Ask for any ADDITIONAL DETAILS.
7. If their number looks like a landline, ask for their MOBILE NUMBER to send the booking link.
8. Thank them and tell them you are texting a booking link to their phone right now.
9. End the call warmly.

EMERGENCY PROTOCOL:
If the caller mentions: gas leak, flooding, burst pipe, house fire, sparking wires, carbon monoxide, 
sewage backup, no heat, or any life-threatening situation:
- Say: "I understand this is an emergency. I am alerting a technician right now. 
  Please call 911 if you are in immediate danger. Someone will contact you within minutes."
- Set urgency to EMERGENCY in your data collection.
- End the call.

DATA TO COLLECT (always extract these, even if the caller doesn't volunteer them directly):
- caller_name: their full name
- service_type: one of the service categories above
- issue_description: brief description of what's wrong
- urgency: "Emergency", "Urgent", or "Not Urgent"
- additional_details: anything else relevant
- mobile_number: the best SMS number for them (may differ from caller_phone)
- lead_score: your estimate — HIGH (job likely over $500), MEDIUM ($150-500), LOW (under $150)

PERSONALITY:
- Warm, professional, efficient
- Never robotic — speak naturally
- If you mishear something, ask once to clarify, then move on
- Keep the call under 3 minutes
- Never make up information about pricing or availability
"""

# ==============================================================================
# IMPORTS
# ==============================================================================

from flask import Flask, request, send_file, Response, jsonify
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Dial
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from openai import OpenAI
from functools import wraps
import atexit
import csv
import hashlib
import hmac
import json
import logging
import os
import threading
import concurrent.futures
from datetime import datetime, timedelta
from urllib.parse import urlencode

app = Flask(__name__)

# ==============================================================================
# LOGGING
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ==============================================================================
# CONFIGURATION
# ==============================================================================

DATA_FILE                 = "/var/data/calls.csv" if os.path.exists("/var/data") else "calls.csv"
OPENAI_API_KEY            = os.environ.get("OPENAI_API_KEY")
openai_client             = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
TWILIO_ACCOUNT_SID        = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN         = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER       = os.environ.get("TWILIO_PHONE_NUMBER")
ELEVENLABS_API_KEY        = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_AGENT_ID       = os.environ.get("ELEVENLABS_AGENT_ID", "")
ELEVENLABS_WEBHOOK_SECRET = os.environ.get("ELEVENLABS_WEBHOOK_SECRET", "")
CALENDLY_LINK             = os.environ.get("CALENDLY_LINK", "https://calendly.com/your-link-here")
BUSINESS_NAME             = os.environ.get("BUSINESS_NAME", "the service desk")
APP_URL                   = os.environ.get("APP_URL", "").rstrip("/")
OWNER_PHONE               = os.environ.get("OWNER_PHONE", "")
DASHBOARD_TOKEN           = os.environ.get("DASHBOARD_TOKEN", "")

executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

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
# SECURITY
# ==============================================================================

def require_dashboard_token(f):
    """Check Authorization: Bearer <token> header for dashboard routes."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not DASHBOARD_TOKEN:
            return f(*args, **kwargs)
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or auth_header[7:] != DASHBOARD_TOKEN:
            return "Unauthorized", 401
        return f(*args, **kwargs)
    return decorated


def verify_elevenlabs_signature(f):
    """
    Verify ElevenLabs post-call webhook signature.
    ElevenLabs signs requests with HMAC-SHA256 using your webhook secret.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not ELEVENLABS_WEBHOOK_SECRET:
            return f(*args, **kwargs)
        signature = request.headers.get("ElevenLabs-Signature", "")
        body = request.get_data()
        expected = hmac.new(
            ELEVENLABS_WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, "sha256=" + expected):
            log.warning("Invalid ElevenLabs webhook signature")
            return "Forbidden", 403
        return f(*args, **kwargs)
    return decorated

# ==============================================================================
# CSV
# ==============================================================================

def ensure_csv_exists():
    try:
        folder = os.path.dirname(DATA_FILE)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if not os.path.exists(DATA_FILE):
            with open(DATA_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "name", "caller_phone", "mobile_phone",
                    "service", "issue", "urgency", "details",
                    "score", "conversation_id", "call_duration_seconds"
                ])
    except Exception as e:
        log.error("CSV init error: %s", e)


def append_to_csv(name, caller, mobile, service, issue, urgency, details, score,
                  conversation_id="", duration=0):
    try:
        with open(DATA_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                name, caller, mobile, service, issue, urgency, details,
                score, conversation_id, duration
            ])
        log.info("Call saved to CSV — %s | %s | %s", name, service, score)
    except Exception as e:
        log.error("CSV write error: %s", e)


ensure_csv_exists()

# ==============================================================================
# SMS
# ==============================================================================

def send_sms(to_number, body):
    try:
        if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
            log.warning("Twilio credentials missing — SMS not sent")
            return False
        if not to_number or to_number in ("Unknown", ""):
            return False
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=body, from_=TWILIO_PHONE_NUMBER, to=to_number)
        log.info("SMS sent to %s", to_number)
        return True
    except Exception as e:
        log.error("SMS failed to %s: %s", to_number, e)
        return False


def send_booking_sms(to_number, name, service):
    body = (
        "Hi " + name + ", thanks for calling " + BUSINESS_NAME + "!\n\n"
        "To book your " + service + " appointment:\n"
        + CALENDLY_LINK + "\n\n"
        "We look forward to helping you!"
    )
    send_sms(to_number, body)


def send_lead_alert(name, caller, service, urgency, issue, details, score, summary=None):
    if not OWNER_PHONE:
        return
    urgency_flag = "🚨 URGENT" if urgency in ("Urgent", "Emergency") else "Standard"
    score_flag   = "🔴 HIGH VALUE" if score == "HIGH" else ("🟡 MID RANGE" if score == "MEDIUM" else "🟢 QUICK JOB")

    if summary:
        body = (
            "New Lead — " + BUSINESS_NAME + "\n\n"
            + summary + "\n\n"
            "Name: " + name + "\n"
            "Phone: " + caller + "\n"
            "Service: " + service + "\n"
            "Urgency: " + urgency_flag + "\n"
            "Value: " + score_flag + "\n\n"
            "Booking link sent to customer."
        )
    else:
        body = (
            "New Lead — " + BUSINESS_NAME + "\n\n"
            "[" + urgency_flag + "] [" + score_flag + "]\n"
            "Name: " + name + "\n"
            "Phone: " + caller + "\n"
            "Service: " + service + "\n"
            "Issue: " + issue + "\n\n"
            "Booking link sent to customer."
        )
    send_sms(OWNER_PHONE, body)


def send_emergency_alert(caller, issue):
    if not OWNER_PHONE:
        return
    body = (
        "🚨 EMERGENCY ALERT — " + BUSINESS_NAME + "\n\n"
        "Phone: " + caller + "\n"
        "Reported: " + issue[:200] + "\n\n"
        "CALL BACK IMMEDIATELY"
    )
    send_sms(OWNER_PHONE, body)

# ==============================================================================
# GPT HELPERS — still used for lead summaries after the call
# ==============================================================================

def gpt_build_lead_summary(name, service, issue, urgency, details, score):
    if not openai_client:
        return None
    try:
        prompt = (
            "Name: " + name + "\n"
            "Service: " + service + "\n"
            "Issue: " + issue + "\n"
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
                        "No greetings. No sign-offs. Be direct."
                    )
                },
                {"role": "user", "content": prompt}
            ]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error("GPT summary error: %s", e)
        return None


def extract_data_from_transcript(transcript_text, data_collection):
    """
    ElevenLabs sends data_collection as a dict where each key contains
    a nested object like: { "value": "John Smith", "rationale": "..." }
    We need to extract the "value" from each field.
    """
    def get_value(field):
        """Extract value from ElevenLabs data collection field."""
        raw = data_collection.get(field, {})
        if isinstance(raw, dict):
            return raw.get("value", "") or ""
        return str(raw) if raw else ""

    name    = get_value("caller_name")
    service = get_value("service_type")
    issue   = get_value("issue_description")
    urgency = get_value("urgency") or "Not Urgent"
    details = get_value("additional_details")
    mobile  = get_value("mobile_number")
    score   = get_value("lead_score") or "MEDIUM"

    # Normalize score
    if score not in ("HIGH", "MEDIUM", "LOW"):
        score = "MEDIUM"

    log.info("Extracted — Name: %s | Service: %s | Urgency: %s | Score: %s",
             name, service, urgency, score)

    return name, service, issue, urgency, details, mobile, score

# ==============================================================================
# NO-SHOW SAVER — confirmation calls (unchanged)
# ==============================================================================

def make_confirmation_call(customer_phone, customer_name, appointment_time, calendly_link):
    try:
        if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, APP_URL]):
            log.warning("Missing credentials for confirmation call")
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
        log.info("Confirmation call to %s SID: %s", customer_phone, call.sid)
    except Exception as e:
        log.error("Confirmation call failed: %s", e)


def schedule_confirmation_call(customer_phone, customer_name, appointment_dt, calendly_link):
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
        log.info("Confirmation scheduled: %s at %s", job_id, call_time)
    except Exception as e:
        log.error("Schedule error: %s", e)

# ==============================================================================
# ROUTES — ELEVENLABS WEBHOOKS (the two new core endpoints)
# ==============================================================================

@app.route("/el-webhook", methods=["POST"])
def el_webhook():
    """
    ElevenLabs calls this at the START of every inbound call.
    We return dynamic variables that personalize the agent for this specific caller.
    ElevenLabs fetches this during the Twilio dialing/connection period — zero added latency.

    Configure in ElevenLabs:
      Agent page → Security → Enable "Fetch conversation initiation data"
      Webhook URL: https://your-app.onrender.com/el-webhook
    """
    try:
        data         = request.get_json(silent=True) or {}
        caller_phone = data.get("caller_id", "Unknown")
        agent_id     = data.get("agent_id", "")
        call_sid     = data.get("call_sid", "")

        log.info("Inbound call from %s | CallSid: %s", caller_phone, call_sid)

        # Return dynamic variables — these fill {{placeholders}} in your agent prompt
        return jsonify({
            "type": "conversation_initiation_client_data",
            "dynamic_variables": {
                "caller_phone":  caller_phone,
                "business_name": BUSINESS_NAME,
                "calendly_link": CALENDLY_LINK,
                "owner_phone":   OWNER_PHONE,
            },
            # Optional: override agent behavior per-call
            # "conversation_config_override": {
            #     "agent": {
            #         "first_message": "Thank you for calling " + BUSINESS_NAME + "! How can I help you today?"
            #     }
            # }
        }), 200

    except Exception as e:
        log.error("EL webhook error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/post-call-webhook", methods=["POST"])
@verify_elevenlabs_signature
def post_call_webhook():
    """
    ElevenLabs calls this AFTER every call ends with the full transcript and extracted data.
    This is where we: save to CSV, send booking SMS, alert the owner.

    Configure in ElevenLabs:
      Workspace Settings → Post-call webhook
      URL: https://your-app.onrender.com/post-call-webhook

    The payload contains:
      - data.conversation_id
      - data.transcript (full conversation text)
      - data.data_collection (structured fields your agent collected)
      - data.analysis (sentiment, summary, etc.)
      - data.metadata (call duration, caller info)
    """
    try:
        payload         = request.get_json(silent=True) or {}
        event_type      = payload.get("type", "")

        # We only care about transcription webhooks
        if event_type != "post_call_transcription":
            return jsonify({"status": "ignored"}), 200

        data            = payload.get("data", {})
        conversation_id = data.get("conversation_id", "")
        transcript      = data.get("transcript", "")
        data_collection = data.get("data_collection", {})
        analysis        = data.get("analysis", {})
        metadata        = data.get("metadata", {})

        # Pull caller phone from multiple possible locations in payload
        twilio_meta  = metadata.get("twilio", {})
        caller_phone = (
            twilio_meta.get("caller_id")
            or twilio_meta.get("From")
            or data.get("caller_id")
            or "Unknown"
        )
        duration = metadata.get("call_duration_secs", 0)

        log.info("Post-call webhook received | Conversation: %s | Duration: %ss | Caller: %s",
                 conversation_id, duration, caller_phone)

        # Log full payload for debugging
        log.info("Data collection raw: %s", data_collection)
        log.info("Metadata raw: %s", metadata)

        # Extract structured data the agent collected during the call
        name, service, issue, urgency, details, mobile, score = extract_data_from_transcript(
            transcript, data_collection
        )

        # Use mobile if collected, otherwise fall back to caller_phone
        sms_target    = mobile if mobile and mobile != "Unknown" else caller_phone
        service_label = service if service else "home service"
        name_label    = name if name else "there"

        log.info("SMS target: %s | Name: %s | Service: %s", sms_target, name_label, service_label)

        # Emergency handling — alert fires immediately
        if urgency == "Emergency":
            executor.submit(send_emergency_alert, caller_phone, issue)

        # Build GPT summary for the owner alert
        summary = gpt_build_lead_summary(name, service, issue, urgency, details, score)

        # Fire everything in parallel — CSV, lead alert, booking SMS
        executor.submit(
            append_to_csv,
            name, caller_phone, sms_target, service,
            issue, urgency, details, score,
            conversation_id, duration
        )
        executor.submit(
            send_lead_alert,
            name_label, caller_phone, service_label, urgency, issue, details, score, summary
        )
        # Always send booking SMS as long as we have a valid phone number
        if sms_target and sms_target != "Unknown":
            executor.submit(send_booking_sms, sms_target, name_label, service_label)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        log.error("Post-call webhook error: %s", e)
        return jsonify({"error": str(e)}), 500

# ==============================================================================
# ROUTES — CALENDLY (unchanged)
# ==============================================================================

@app.route("/calendly-webhook", methods=["POST"])
def calendly_webhook():
    """
    Triggered when customer books via Calendly.
    Set in Calendly: Integrations → Webhooks → invitee.created
    URL: https://your-app.onrender.com/calendly-webhook
    """
    try:
        data           = request.get_json(silent=True) or {}
        payload        = data.get("payload", {})
        event          = payload.get("event", {})
        invitee        = payload.get("invitee", {})
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
                "Appointment Booked — " + BUSINESS_NAME + "\n\n"
                "Customer: " + customer_name + "\n"
                "Service: " + event_name + "\n"
                "Time: " + formatted_time
            )
            if customer_phone:
                body += "\nPhone: " + customer_phone
            body += "\n\nConfirmation call scheduled for day before."
            executor.submit(send_sms, OWNER_PHONE, body)

        if customer_phone:
            schedule_confirmation_call(customer_phone, customer_name, appointment_dt, CALENDLY_LINK)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        log.error("Calendly webhook error: %s", e)
        return jsonify({"status": "error"}), 500

# ==============================================================================
# ROUTES — CONFIRMATION CALLS (unchanged)
# ==============================================================================

@app.route("/confirm-appointment", methods=["GET", "POST"])
def confirm_appointment():
    from twilio.twiml.voice_response import Gather
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
        "Hi " + name + ", this is " + BUSINESS_NAME + " calling to confirm your appointment "
        "scheduled for " + appointment_time + ". "
        "Press 1 to confirm or press 2 to reschedule.",
        voice="Polly.Joanna", language="en-US"
    )
    response.append(gather)
    response.say("We did not receive a response. Please call us back. Goodbye.",
                 voice="Polly.Joanna", language="en-US")
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
            "Perfect " + name + "! Your appointment for " + appointment_time +
            " is confirmed. See you then. Goodbye!",
            voice="Polly.Joanna", language="en-US"
        )
        response.hangup()
        if OWNER_PHONE:
            executor.submit(send_sms, OWNER_PHONE,
                "Appointment Confirmed — " + BUSINESS_NAME + "\n\n"
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
        executor.submit(send_sms, phone,
            "Hi " + name + "! No problem at all.\n\n"
            "Click below to reschedule:\n" + calendly + "\n\n"
            "We look forward to seeing you!"
        )
        if OWNER_PHONE:
            executor.submit(send_sms, OWNER_PHONE,
                "Reschedule Requested — " + BUSINESS_NAME + "\n\n"
                "Customer: " + name + "\n"
                "Original: " + appointment_time + "\n"
                "Phone: " + phone
            )
    else:
        response.say("We did not receive a response. Please call us back. Goodbye.",
                     voice="Polly.Joanna", language="en-US")
        response.hangup()

    return str(response)

# ==============================================================================
# ROUTES — DASHBOARD (unchanged, improved auth)
# ==============================================================================

@app.route("/", methods=["GET"])
def home():
    return "AI Receptionist (ElevenLabs Agent Edition) is running."


@app.route("/check-csv", methods=["GET"])
@require_dashboard_token
def check_csv():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return "<pre>" + f.read() + "</pre>"
    return "CSV not found.", 404


@app.route("/download-csv", methods=["GET"])
@require_dashboard_token
def download_csv():
    if os.path.exists(DATA_FILE):
        return send_file(DATA_FILE, as_attachment=True)
    return "CSV not found.", 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
