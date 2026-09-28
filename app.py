import os
import time
import threading
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

# Stripe rejects card charges below $0.50 USD. The "starts at a penny" idea
# can't survive contact with that rule, so this is the real floor for the
# price ladder.
MIN_CHARGE_CENTS = 50

if not STRIPE_SECRET_KEY or not STRIPE_PUBLISHABLE_KEY:
    print("WARNING: Missing Stripe keys. Set STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY in .env.")

stripe.api_key = STRIPE_SECRET_KEY

app = Flask(__name__)

if FORCE_HTTPS:
    # Trust proxy headers for correct url_for with _external
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        PREFERRED_URL_SCHEME="https",
    )

# --- Database setup ---
# DB_PATH points at a plain file path, not a sqlite:// URL, so it's an easy
# thing to set in Render's dashboard: point it at your attached disk's mount
# path (e.g. /var/data/pennycelebrity.db) and the database survives redeploys.
# Left unset, it falls back to a file in the working directory, which is fine
# for local development but is NOT persistent on Render without a disk.
DB_PATH = os.getenv("DB_PATH", "pennycelebrity.db")
DB_URL = f"sqlite:///{DB_PATH}"

class Base(DeclarativeBase):
    pass

class Settings(Base):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_amount_cents: Mapped[int] = mapped_column(Integer, default=MIN_CHARGE_CENTS)
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

# Belt-and-suspenders uniqueness on payment intent, added as a raw index so
# it also applies to a database file that already existed before this
# constraint was added (create_all() won't retrofit it onto an existing
# table). This is the last line of defense against double-processing the
# same Stripe payment if a webhook is ever delivered twice.
with engine.begin() as conn:
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_celebrities_payment_intent "
        "ON celebrities (stripe_payment_intent)"
    )

def get_or_create_settings(sess: Session) -> Settings:
    s = sess.get(Settings, 1)
    if not s:
        s = Settings(id=1, next_amount_cents=MIN_CHARGE_CENTS)
        sess.add(s)
        sess.commit()
        sess.refresh(s)
    elif s.next_amount_cents < MIN_CHARGE_CENTS:
        # Guards against a pre-existing DB (or a manual edit) leaving the
        # price below what Stripe will actually let anyone pay.
        s.next_amount_cents = MIN_CHARGE_CENTS
        sess.commit()
        sess.refresh(s)
    return s


# --- Helpers ---
def now_utc():
    return datetime.now(timezone.utc)

def as_utc(dt):
    # SQLite has no real timezone-aware storage: SQLAlchemy's DateTime(timezone=True)
    # writes an ISO string but reads it back *naive*. Values that were computed
    # in-process (e.g. via now_utc()) stay tz-aware, so mixing a freshly-read row
    # with a fresh now_utc() call raises "can't subtract offset-naive and
    # offset-aware datetimes". Everything in this app is UTC by convention, so
    # we just re-attach that tzinfo on the way out of the DB.
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

@app.context_processor
def inject_template_globals():
    return {"current_year": now_utc().year}

# Minimal in-process rate limit for checkout-session creation. This is not
# shared across gunicorn workers/processes, so it's a speed bump against
# accidental double-submits and light abuse, not a real anti-fraud control.
_last_checkout_request = {}
_rate_lock = threading.Lock()
CHECKOUT_RATE_LIMIT_SECONDS = 3

def _is_rate_limited(client_ip: str) -> bool:
    now_ts = time.time()
    with _rate_lock:
        last = _last_checkout_request.get(client_ip, 0)
        if now_ts - last < CHECKOUT_RATE_LIMIT_SECONDS:
            return True
        _last_checkout_request[client_ip] = now_ts
    return False

# --- Routes ---
@app.get("/")
def index():
    with Session(engine) as sess:
        s = get_or_create_settings(sess)
        # current celeb = most recent with end_time is NULL
        current = sess.execute(
            select(Celebrity).where(Celebrity.end_time.is_(None)).order_by(Celebrity.start_time.desc())
        ).scalars().first()
        if current:
            # Re-attach UTC tzinfo (see as_utc) so the ISO string this renders
            # into the page carries an explicit offset — otherwise the
            # in-page JS timer parses it as the visitor's local time instead
            # of UTC and shows the wrong "time on top".
            current.start_time = as_utc(current.start_time)
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
            c.start_time = as_utc(c.start_time)
            c.end_time = as_utc(c.end_time)
            end = c.end_time or n
            duration_s = int((end - c.start_time).total_seconds())
            rows.append((c, duration_s))
        # sort by duration desc
        rows.sort(key=lambda t: t[1], reverse=True)
        top = rows[:50]
        return render_template("leaderboard.html", top=top)

@app.get("/claim")
def claim():
    # The claim form now lives inline on the homepage. This route stays so
    # old bookmarks/links don't break, and just sends people to the form.
    return redirect(url_for("index", _anchor="claim"), code=301)

@app.post("/create-checkout-session")
def create_checkout_session():
    client_ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown").split(",")[0].strip()
    if _is_rate_limited(client_ip):
        return jsonify(error="Too many requests — please wait a few seconds and try again."), 429

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

        with Session(engine) as sess:
            # Idempotency: Stripe can and does redeliver webhooks (retries,
            # timeouts, manual resends). If we've already recorded a win for
            # this payment intent, don't process it again.
            already = sess.execute(
                select(Celebrity.id).where(Celebrity.stripe_payment_intent == payment_intent_id)
            ).scalars().first()
            if already:
                return "already processed", 200

            s = get_or_create_settings(sess)
            current_required = s.next_amount_cents

            if amount_total != current_required:
                # Amount mismatch -> refund. The idempotency key means a
                # redelivered webhook for the same payment intent can't
                # trigger a second refund attempt.
                stripe.Refund.create(
                    payment_intent=payment_intent_id,
                    idempotency_key=f"refund-mismatch-{payment_intent_id}",
                )
                return "refunded due to race", 200

            # Atomic compare-and-swap on the price itself: the UPDATE only
            # matches (and only takes effect) if next_amount_cents is still
            # what we just read. If a concurrent webhook already bumped it,
            # rowcount is 0 and we know we lost the race, without needing
            # row-level locking that SQLite doesn't support.
            result = sess.execute(
                update(Settings)
                .where(Settings.id == 1, Settings.next_amount_cents == current_required)
                .values(next_amount_cents=current_required + 1)
            )
            if result.rowcount == 0:
                stripe.Refund.create(
                    payment_intent=payment_intent_id,
                    idempotency_key=f"refund-race-{payment_intent_id}",
                )
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

            try:
                sess.commit()
            except IntegrityError:
                # Belt-and-suspenders: a second delivery of the same event
                # raced us between the "already processed" check above and
                # this commit, and tripped the unique index instead.
                sess.rollback()
                return "already processed", 200

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
