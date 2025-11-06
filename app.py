import os
import time
import stripe
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
from sqlalchemy import create_engine, Integer, String, DateTime, Text, func, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv
from moderation import sanitize_name

load_dotenv()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
SITE_URL = os.getenv("SITE_URL", "http://localhost:5000")
FORCE_HTTPS = os.getenv("FORCE_HTTPS", "0") == "1"

if not STRIPE_SECRET_KEY or not STRIPE_PUBLISHABLE_KEY:
    print("WARNING: Missing Stripe keys. Set STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY in .env.")

stripe.api_key = STRIPE_SECRET_KEY

app = Flask(__name__)

@app.context_processor
def inject_current_year():
    from datetime import datetime
    return {"current_year": datetime.utcnow().year}



if FORCE_HTTPS:
    # Trust proxy headers for correct url_for with _external
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        PREFERRED_URL_SCHEME="https",
    )

# --- Database setup ---
DB_URL = "sqlite:///pennycelebrity.db"

class Base(DeclarativeBase):
    pass

class Settings(Base):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_amount_cents: Mapped[int] = mapped_column(Integer, default=50)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Celebrity(Base):
    __tablename__ = "celebrities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    amount_cents: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stripe_payment_intent: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40), default="succeeded")  # or refunded
    raw_name: Mapped[str] = mapped_column(Text)

engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)

def get_or_create_settings(sess: Session) -> Settings:
    s = sess.get(Settings, 1)
    if not s:
        s = Settings(id=1, next_amount_cents=50)
        sess.add(s)
        sess.commit()
        sess.refresh(s)
    # Enforce Stripe minimum (USD $0.50)
    if s.next_amount_cents < 50:
        s.next_amount_cents = 50
        sess.commit()
    return s


# --- Helpers ---
def now_utc():
    return datetime.now(timezone.utc)

# --- Routes ---
@app.get("/")
def index():
    with Session(engine) as sess:
        s = get_or_create_settings(sess)
        # current celeb = most recent with end_time is NULL
        current = sess.execute(
            select(Celebrity).where(Celebrity.end_time.is_(None)).order_by(Celebrity.start_time.desc())
        ).scalars().first()
        return render_template("index.html",
                               publishable_key=STRIPE_PUBLISHABLE_KEY,
                               next_amount_cents=s.next_amount_cents,
                               current=current,
                               site_url=SITE_URL)

@app.get("/leaderboard")
def leaderboard():
    with Session(engine) as sess:
        # Calculate durations; if end_time is NULL, use now
        celebs = sess.execute(
            select(Celebrity).order_by(Celebrity.start_time.desc())
        ).scalars().all()

        # compute duration seconds for sort
        rows = []
        n = now_utc()
        for c in celebs:
            end = c.end_time or n
            duration_s = int((end - c.start_time).total_seconds())
            rows.append((c, duration_s))
        # sort by duration desc
        rows.sort(key=lambda t: t[1], reverse=True)
        top = rows[:50]
        return render_template("leaderboard.html", top=top)

@app.get("/claim")
def claim():
    with Session(engine) as sess:
        s = get_or_create_settings(sess)
        return render_template("claim.html", next_amount_cents=s.next_amount_cents, publishable_key=STRIPE_PUBLISHABLE_KEY)

@app.post("/create-checkout-session")
def create_checkout_session():
    raw_name = request.form.get("name", "").strip()
    sanitized = sanitize_name(raw_name)

    with Session(engine) as sess:
        s = get_or_create_settings(sess)
        amount_cents = s.next_amount_cents  # snapshot
    # Create Stripe Checkout Session with dynamic price
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Penny Celebrity Claim"},
                    "unit_amount": amount_cents,
                },
                "quantity": 1,
            }],
            metadata={
                "display_name": sanitized,
                "raw_name": raw_name,
                "expected_amount_cents": str(amount_cents),
            },
            success_url=f"{SITE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{SITE_URL}/cancel",
            allow_promotion_codes=False,
        )
        return redirect(session.url, code=303)
    except Exception as e:
        return jsonify(error=str(e)), 400

@app.get("/success")
def success():
    session_id = request.args.get("session_id")
    return render_template("success.html", session_id=session_id)

@app.get("/cancel")
def cancel():
    return render_template("cancel.html")

@app.post("/webhook")
def webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    if not STRIPE_WEBHOOK_SECRET:
        abort(400, "Webhook secret not configured")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        return str(e), 400

    if event["type"] == "checkout.session.completed":
        sess_obj = event["data"]["object"]
        payment_intent_id = sess_obj.get("payment_intent")
        amount_total = sess_obj.get("amount_total")
        meta = sess_obj.get("metadata", {}) or {}
        raw_name = meta.get("raw_name", "") or ""
        display_name = meta.get("display_name", "") or "Anonymous"
        expected_amount = int(meta.get("expected_amount_cents") or 0)

        # Double-check payment intent status
        pi = stripe.PaymentIntent.retrieve(payment_intent_id)
        if pi.status != "succeeded":
            # ignore non-succeeded
            return "ignored", 200

        # Transaction: compare-and-swap the price
        with Session(engine) as sess:
            s = get_or_create_settings(sess)
            current_required = s.next_amount_cents

            if amount_total != current_required:
                # Amount mismatch -> refund
                stripe.Refund.create(payment_intent=payment_intent_id)
                sess.commit()
                return "refunded due to race", 200

            # Close current celeb (if any)
            current = sess.execute(
                select(Celebrity).where(Celebrity.end_time.is_(None)).order_by(Celebrity.start_time.desc())
            ).scalars().first()
            if current:
                current.end_time = now_utc()

            # Insert new celeb
            c = Celebrity(
                name=display_name,
                raw_name=raw_name,
                amount_cents=amount_total,
                start_time=now_utc(),
                stripe_payment_intent=payment_intent_id,
                status="succeeded"
            )
            sess.add(c)
            # Increment next price by 1 cent
            s.next_amount_cents = current_required + 1

            sess.commit()
        return "ok", 200

    return "ignored", 200

@app.template_filter("usd")
def usd(cents: int):
    return f"${cents/100:.2f}"

@app.template_filter("duration")
def duration(seconds: int):
    # HH:MM:SS
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

if __name__ == "__main__":
    app.run(debug=True)
