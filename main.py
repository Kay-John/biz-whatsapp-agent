import os
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, request
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from anthropic import Anthropic
from supabase import create_client

from client_config import BUSINESS_NAME, OWNER_PHONE, SYSTEM_PROMPT, FOLLOW_UP_MESSAGE

app = Flask(__name__)

ai = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

WHATSAPP_FROM = os.environ["TWILIO_WHATSAPP_FROM"]
SMS_FROM = os.environ.get("TWILIO_SMS_FROM", "")
CLIENT_ID = os.environ["CLIENT_ID"]  # unique ID per client deployment, stored in bot_clients table


# ── Subscription check ────────────────────────────────────────────────────────

_subscription_cache = {"status": None, "checked_at": None}
_expiry_notified = {"warned": False, "expired": False}


def get_subscription():
    """Return subscription record, cached for 30 minutes."""
    now = datetime.now(timezone.utc)
    cache = _subscription_cache
    if cache["checked_at"] and (now - cache["checked_at"]).seconds < 1800:
        return cache["status"]
    r = sb.table("bot_clients").select("*").eq("client_id", CLIENT_ID).execute()
    record = r.data[0] if r.data else None
    cache["status"] = record
    cache["checked_at"] = now
    return record


def check_subscription():
    """
    Returns (is_active, days_remaining).
    Also sends expiry warnings to owner when needed.
    """
    record = get_subscription()
    if not record:
        return False, 0

    expires_at = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    days_remaining = (expires_at - now).days

    owner_wa = f"whatsapp:+256{OWNER_PHONE.lstrip('0')}"

    # 3-day warning (send once)
    if 0 < days_remaining <= 3 and not _expiry_notified["warned"]:
        _expiry_notified["warned"] = True
        try:
            twilio.messages.create(
                body=(
                    f"⚠️ *{BUSINESS_NAME} WhatsApp Bot — Subscription Expiring Soon*\n\n"
                    f"Your bot subscription expires in *{days_remaining} day(s)*.\n\n"
                    f"Please contact your agent to renew and avoid any service interruption. 🙏"
                ),
                from_=WHATSAPP_FROM,
                to=owner_wa
            )
        except Exception as e:
            print(f"Warning notification failed: {e}")

    # Expired
    if days_remaining <= 0:
        if not _expiry_notified["expired"]:
            _expiry_notified["expired"] = True
            try:
                twilio.messages.create(
                    body=(
                        f"🔴 *{BUSINESS_NAME} WhatsApp Bot — Subscription Expired*\n\n"
                        f"Your bot subscription has expired. The bot is now *paused*.\n\n"
                        f"Contact your agent to renew and restore the service. 📞"
                    ),
                    from_=WHATSAPP_FROM,
                    to=owner_wa
                )
            except Exception as e:
                print(f"Expiry notification failed: {e}")
        return False, days_remaining

    return True, days_remaining


# ── Database helpers ──────────────────────────────────────────────────────────

def get_or_create_lead(phone):
    r = sb.table("bot_leads").select("*").eq("phone", phone).eq("client_id", CLIENT_ID).execute()
    if r.data:
        return r.data[0]
    r = sb.table("bot_leads").insert({
        "phone": phone,
        "client_id": CLIENT_ID,
        "status": "new",
        "conversation_history": [],
        "opted_in": True
    }).execute()
    return r.data[0]


def update_lead(phone, data):
    data["last_message_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("bot_leads").update(data).eq("phone", phone).eq("client_id", CLIENT_ID).execute()


# ── Notifications ─────────────────────────────────────────────────────────────

def send_sms_alert(message):
    """Send SMS alert to business owner."""
    owner = f"+256{OWNER_PHONE.lstrip('0')}"
    sms_from = os.environ.get("TWILIO_SMS_FROM", "")
    if not sms_from:
        print(f"[ALERT — set TWILIO_SMS_FROM to enable SMS]: {message}")
        return
    try:
        twilio.messages.create(body=message, from_=sms_from, to=owner)
    except Exception as e:
        print(f"SMS alert failed: {e}")


# ── AI response ───────────────────────────────────────────────────────────────

def get_ai_reply(history, user_message):
    clean_history = [m for m in history if m.get("content", "").strip()]
    messages = clean_history + [{"role": "user", "content": user_message}]
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

    resp = MessagingResponse()

    # Ignore empty text messages (e.g. stickers sent without caption)
    if not body and num_media == 0:
        return str(resp)

    # Subscription gate — disabled during testing, enable before going live
    # is_active, _ = check_subscription()
    # if not is_active:
    #     resp.message(
    #         "Sorry, this service is temporarily unavailable. Please try again later. 🙏"
    #     )
    #     return str(resp)

    lead = get_or_create_lead(phone)
    history = lead.get("conversation_history") or []
    status = lead.get("status", "new")

    # Opt-out
    if body.lower() in ["stop", "unsubscribe", "quit"]:
        update_lead(phone, {"opted_in": False, "status": "opted_out"})
        resp.message(f"You've been unsubscribed from {BUSINESS_NAME} messages. Reply START to re-subscribe anytime.")
        return str(resp)

    # Re-subscribe
    if body.lower() == "start" and status == "opted_out":
        update_lead(phone, {"opted_in": True, "status": "new"})
        resp.message(f"Welcome back! 😊 You're re-subscribed to {BUSINESS_NAME} updates. How can I help you today?")
        return str(resp)

    # Screenshot received
    if num_media > 0 and status in ["awaiting_screenshot", "interested", "joined", "new"]:
        update_lead(phone, {"status": "screenshot_received", "screenshot_url": media_url})
        send_sms_alert(
            f"✅ NEW MEMBER VERIFIED — {BUSINESS_NAME}\n"
            f"Phone: {phone}\n"
            f"Dashboard screenshot received.\n"
            f"Screenshot: {media_url}\n"
            f"Action: Verify their referral on your dashboard."
        )
        resp.message(
            "Thank you for the screenshot! 🎉\n\n"
            "Our team will verify your registration shortly. "
            f"Welcome to the {BUSINESS_NAME} family! 🌟\n\n"
            "Feel free to ask if you need help getting started. 💰"
        )
        return str(resp)

    # AI response
    reply = get_ai_reply(history, body)

    history = (history + [
        {"role": "user", "content": body},
        {"role": "assistant", "content": reply}
    ])[-20:]

    if user_confirmed_joining(body) and status not in ["screenshot_received", "awaiting_screenshot"]:
        status = "awaiting_screenshot"
        send_sms_alert(
            f"🔔 NEW SIGNUP — {BUSINESS_NAME}\n"
            f"Phone: {phone}\n"
            f"Confirmed joining and deposit. Waiting for dashboard screenshot."
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
            is_active = True  # subscription check disabled during testing
            if is_active:
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
                leads = (
                    sb.table("bot_leads")
                    .select("*")
                    .eq("client_id", CLIENT_ID)
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
                        sb.table("bot_leads").update({"followed_up": True}).eq("phone", phone).eq("client_id", CLIENT_ID).execute()
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
