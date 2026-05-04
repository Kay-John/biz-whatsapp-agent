import os
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, request
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from anthropic import Anthropic
from supabase import create_client

from client_config import BUSINESS_NAME, OWNER_PHONE, REGISTRATION_LINK, SYSTEM_PROMPT, FOLLOW_UP_MESSAGE

app = Flask(__name__)

ai = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

WHATSAPP_FROM = os.environ["TWILIO_WHATSAPP_FROM"]
SMS_FROM = os.environ.get("TWILIO_SMS_FROM", "")


# ── Database helpers ─────────────────────────────────────────────────────────

def get_or_create_lead(phone):
    r = sb.table("starhela_leads").select("*").eq("phone", phone).execute()
    if r.data:
        return r.data[0]
    r = sb.table("starhela_leads").insert({
        "phone": phone,
        "status": "new",
        "conversation_history": [],
        "opted_in": True
    }).execute()
    return r.data[0]


def update_lead(phone, data):
    data["last_message_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("starhela_leads").update(data).eq("phone", phone).execute()


# ── Notifications ─────────────────────────────────────────────────────────────

def send_sms_alert(message):
    owner = f"+256{OWNER_PHONE.lstrip('0')}"
    if not SMS_FROM:
        print(f"[SMS ALERT — no TWILIO_SMS_FROM set]: {message}")
        return
    try:
        twilio.messages.create(body=message, from_=SMS_FROM, to=owner)
    except Exception as e:
        print(f"SMS alert failed: {e}")


# ── AI response ───────────────────────────────────────────────────────────────

def get_ai_reply(history, user_message):
    messages = list(history) + [{"role": "user", "content": user_message}]
    r = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    return r.content[0].text


# ── Intent detection ──────────────────────────────────────────────────────────

JOINED_KEYWORDS = [
    "joined", "registered", "signed up", "sign up", "created account",
    "made deposit", "deposited", "done", "i have joined", "nimejiunge",
    "nimefanya", "account created", "i registered", "i've joined",
    "nimejoin", "niko", "nimemaliza"
]

def user_confirmed_joining(text):
    t = text.lower()
    return any(k in t for k in JOINED_KEYWORDS)


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From", "").replace("whatsapp:", "")
    body = request.form.get("Body", "").strip()
    num_media = int(request.form.get("NumMedia", 0))
    media_url = request.form.get("MediaUrl0", "")

    lead = get_or_create_lead(phone)
    history = lead.get("conversation_history") or []
    status = lead.get("status", "new")

    resp = MessagingResponse()

    # Opt-out
    if body.lower() in ["stop", "unsubscribe", "quit"]:
        update_lead(phone, {"opted_in": False, "status": "opted_out"})
        resp.message("You've been unsubscribed from Starhela messages. Reply START to re-subscribe anytime.")
        return str(resp)

    # Re-subscribe
    if body.lower() == "start" and status == "opted_out":
        update_lead(phone, {"opted_in": True, "status": "new"})
        resp.message("Welcome back! 😊 You're re-subscribed to Starhela updates. How can I help you today?")
        return str(resp)

    # Dashboard screenshot received
    if num_media > 0 and status in ["awaiting_screenshot", "interested", "joined", "new"]:
        update_lead(phone, {
            "status": "screenshot_received",
            "screenshot_url": media_url
        })
        send_sms_alert(
            f"✅ NEW STARHELA MEMBER VERIFIED\n"
            f"Phone: {phone}\n"
            f"Dashboard screenshot received.\n"
            f"Screenshot URL: {media_url}\n"
            f"Action: Verify their referral on your Starhela dashboard."
        )
        resp.message(
            "Thank you for the screenshot! 🎉\n\n"
            "Our team will verify your registration shortly. "
            "Welcome to the Starhela family! 🌟\n\n"
            "Feel free to ask anytime if you need help getting started with any of the earning streams. 💰"
        )
        return str(resp)

    # Get AI response
    reply = get_ai_reply(history, body)

    # Update conversation history — keep last 20 messages
    history = history + [
        {"role": "user", "content": body},
        {"role": "assistant", "content": reply}
    ]
    history = history[-20:]

    # Detect joining confirmation
    if user_confirmed_joining(body) and status not in ["screenshot_received", "awaiting_screenshot"]:
        status = "awaiting_screenshot"
        send_sms_alert(
            f"🔔 NEW STARHELA SIGNUP\n"
            f"Phone: {phone}\n"
            f"They've confirmed joining and depositing.\n"
            f"Waiting for dashboard screenshot to verify referral."
        )
    elif status == "new":
        status = "interested"

    update_lead(phone, {"conversation_history": history, "status": status})

    resp.message(reply)
    return str(resp)


# ── Follow-up engine ──────────────────────────────────────────────────────────

def follow_up_engine():
    while True:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            leads = (
                sb.table("starhela_leads")
                .select("*")
                .eq("followed_up", False)
                .eq("opted_in", True)
                .in_("status", ["new", "interested"])
                .lt("last_message_at", cutoff)
                .execute()
            )
            for lead in leads.data:
                phone = lead["phone"]
                try:
                    twilio.messages.create(
                        body=FOLLOW_UP_MESSAGE,
                        from_=WHATSAPP_FROM,
                        to=f"whatsapp:{phone}"
                    )
                    sb.table("starhela_leads").update({"followed_up": True}).eq("phone", phone).execute()
                    print(f"Follow-up sent to {phone}")
                except Exception as e:
                    print(f"Follow-up failed for {phone}: {e}")
        except Exception as e:
            print(f"Follow-up engine error: {e}")
        time.sleep(3600)


threading.Thread(target=follow_up_engine, daemon=True).start()


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return f"{BUSINESS_NAME} WhatsApp Agent is live. ✅"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
