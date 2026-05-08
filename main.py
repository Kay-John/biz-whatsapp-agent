import os
import threading
import time
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, request, session, redirect, render_template, send_from_directory
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from anthropic import Anthropic
from supabase import create_client

from client_config import BUSINESS_NAME, OWNER_PHONE, SYSTEM_PROMPT, FOLLOW_UP_MESSAGE

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "starhela-secret-2026")

ai = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

WHATSAPP_FROM = os.environ["TWILIO_WHATSAPP_FROM"]
CLIENT_ID = os.environ["CLIENT_ID"]
DASH_USER = os.environ.get("DASH_USERNAME", "Jusper001")
DASH_PASS = os.environ.get("DASH_PASSWORD", "admin256")

OUTREACH_MESSAGE = (
    "👋 Hi! I came across your number and wanted to share something exciting.\n\n"
    "I'm reaching out from *Starhela* — a digital earning platform where members "
    "earn through surveys, tasks, games, and referrals. 💰\n\n"
    "You can start earning today with just *$5*.\n\n"
    "Interested to learn more? Just reply and I'll walk you through everything! 😊"
)


# ── Auth decorator ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect("/dashboard/login")
        return f(*args, **kwargs)
    return decorated


# ── Subscription check ────────────────────────────────────────────────────────

_subscription_cache = {"status": None, "checked_at": None}
_expiry_notified = {"warned": False, "expired": False}


def get_subscription():
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
    record = get_subscription()
    if not record:
        return False, 0

    expires_at = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    days_remaining = (expires_at - now).days

    owner_wa = f"whatsapp:+256{OWNER_PHONE.lstrip('0')}"

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


# ── WhatsApp Webhook ──────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From", "").replace("whatsapp:", "")
    body = request.form.get("Body", "").strip()
    num_media = int(request.form.get("NumMedia", 0))
    media_url = request.form.get("MediaUrl0", "")

    resp = MessagingResponse()

    if not body and num_media == 0:
        return str(resp)

    # Subscription gate — disabled during testing, enable before going live
    # is_active, _ = check_subscription()
    # if not is_active:
    #     resp.message("Sorry, this service is temporarily unavailable. Please try again later. 🙏")
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
    if num_media > 0:
        update_lead(phone, {"status": "screenshot_received", "screenshot_url": media_url})
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
    elif status == "new":
        status = "interested"

    update_lead(phone, {"conversation_history": history, "status": status})
    resp.message(reply)
    return str(resp)


# ── Subscribe page ────────────────────────────────────────────────────────────

@app.route("/subscribe", methods=["GET", "POST"])
def subscribe():
    if request.method == "POST":
        name      = request.form.get("name", "")
        phone     = request.form.get("phone", "")
        method    = request.form.get("method", "")
        amount    = request.form.get("amount", "")
        reference = request.form.get("reference", "")
        message   = request.form.get("message", "")

        sb.table("payment_submissions").insert({
            "name": name, "phone": phone, "method": method,
            "amount": amount, "reference": reference,
            "message": message, "client_id": CLIENT_ID,
            "submitted_at": datetime.now(timezone.utc).isoformat()
        }).execute()

        try:
            twilio.messages.create(
                body=(
                    f"💰 *NEW PAYMENT SUBMISSION — {BUSINESS_NAME}*\n\n"
                    f"Name: {name}\n"
                    f"Phone: {phone}\n"
                    f"Method: {method}\n"
                    f"Amount: {amount}\n"
                    f"Reference: {reference}\n"
                    f"Message: {message or '—'}\n\n"
                    f"Action: Verify payment and activate their subscription in Supabase."
                ),
                from_=WHATSAPP_FROM,
                to="whatsapp:+256793482095"
            )
        except Exception as e:
            print(f"Payment notification failed: {e}")

        return render_template("subscribe.html", success=True)
    return render_template("subscribe.html", success=False)


# ── Dashboard auth ────────────────────────────────────────────────────────────

@app.route("/dashboard/login", methods=["GET", "POST"])
def dash_login():
    if request.method == "POST":
        if request.form.get("username") == DASH_USER and request.form.get("password") == DASH_PASS:
            session["logged_in"] = True
            return redirect("/dashboard")
        return render_template("dash_login.html", error="Invalid username or password.")
    return render_template("dash_login.html", error=None)


@app.route("/dashboard/logout")
def dash_logout():
    session.clear()
    return redirect("/dashboard/login")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    leads = sb.table("bot_leads").select("*").eq("client_id", CLIENT_ID).order("last_message_at", desc=True).execute().data or []
    payments = sb.table("payment_submissions").select("*").eq("client_id", CLIENT_ID).order("submitted_at", desc=True).execute().data or []

    stats = {
        "total": len(leads),
        "interested": sum(1 for l in leads if l.get("status") == "interested"),
        "awaiting": sum(1 for l in leads if l.get("status") == "awaiting_screenshot"),
        "joined": sum(1 for l in leads if l.get("status") == "screenshot_received"),
        "payments": len(payments)
    }

    return render_template(
        "dashboard.html",
        leads=leads,
        payments=payments,
        stats=stats,
        message=request.args.get("msg"),
        error=request.args.get("err")
    )


@app.route("/dashboard/send", methods=["POST"])
@login_required
def dashboard_send():
    phone = request.form.get("phone", "").strip().lstrip("+")
    if not phone:
        return redirect("/dashboard?err=Phone+number+is+required")
    try:
        twilio.messages.create(
            body=OUTREACH_MESSAGE,
            from_=WHATSAPP_FROM,
            to=f"whatsapp:+{phone}"
        )
        r = sb.table("bot_leads").select("id").eq("phone", f"+{phone}").eq("client_id", CLIENT_ID).execute()
        if not r.data:
            sb.table("bot_leads").insert({
                "phone": f"+{phone}",
                "client_id": CLIENT_ID,
                "status": "new",
                "conversation_history": [],
                "opted_in": True
            }).execute()
        return redirect(f"/dashboard?msg=Message+sent+to+%2B{phone}")
    except Exception as e:
        return redirect(f"/dashboard?err={str(e)[:80]}")


@app.route("/dashboard/send-bulk", methods=["POST"])
@login_required
def dashboard_send_bulk():
    raw = request.form.get("phones", "")
    phones = [p.strip().lstrip("+") for p in raw.splitlines() if p.strip()][:50]
    sent, failed = 0, 0
    for phone in phones:
        try:
            twilio.messages.create(
                body=OUTREACH_MESSAGE,
                from_=WHATSAPP_FROM,
                to=f"whatsapp:+{phone}"
            )
            r = sb.table("bot_leads").select("id").eq("phone", f"+{phone}").eq("client_id", CLIENT_ID).execute()
            if not r.data:
                sb.table("bot_leads").insert({
                    "phone": f"+{phone}",
                    "client_id": CLIENT_ID,
                    "status": "new",
                    "conversation_history": [],
                    "opted_in": True
                }).execute()
            sent += 1
        except:
            failed += 1
    return redirect(f"/dashboard?msg=Sent+{sent}+messages.+{failed}+failed.")


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


# ── Static & home ─────────────────────────────────────────────────────────────

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


@app.route("/")
def home():
    return f"{BUSINESS_NAME} WhatsApp Agent is live. ✅ | <a href='/subscribe'>Subscribe</a> | <a href='/dashboard'>Dashboard</a>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
