import os
import time
import threading
from io import BytesIO
import stripe
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
from sqlalchemy import create_engine, Integer, String, DateTime, Text, Boolean, func, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageFilter
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

# Once the price would climb past this, the ladder resets back to the floor
# instead. An unbounded ladder eventually prices out everyone and the site
# just goes quiet with an unreachable champion; capping and resetting keeps
# the cheap, impulse-buy entry point coming back.
MAX_CHARGE_CENTS = 2000  # $20.00

# If nobody claims for this long, the price resets back to the floor even if
# it never reached the ceiling above — an expensive, stalled ladder is just
# as dead as an unbounded one.
INACTIVITY_RESET_HOURS = 48

# Whoever pays the $20 ceiling closes out a round. Without some payoff for
# that, it's a bad deal: they paid the most and can be dethroned by the very
# next $0.50 bidder. This guarantees them real, uninterrupted spotlight time
# — nobody (not even a matching payment) can dethrone them until it elapses
# — plus a permanent listing as a Round Champion (see is_round_champion).
CHAMPION_HOLD_MINUTES = 30

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
    last_claim_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set only when the current celebrity paid the $20 ceiling. Nobody —
    # not even a payment at the correct price — can dethrone them until
    # this passes. None/past means there's no active protection.
    champion_lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    # True when this reign was claimed at the $20 ceiling price — i.e. this
    # person closed out a round. Drives the separate Round Champions listing.
    is_round_champion: Mapped[bool] = mapped_column(Boolean, default=False)

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
    # create_all() only creates missing TABLES, it won't retrofit a new
    # COLUMN onto a settings table that already exists on a live database
    # (like the one on the deployed disk). Add last_claim_at by hand if an
    # older deploy's schema doesn't have it yet, so the inactivity-reset
    # feature works without needing to wipe the existing database.
    existing_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(settings)").fetchall()}
    if "last_claim_at" not in existing_columns:
        conn.exec_driver_sql("ALTER TABLE settings ADD COLUMN last_claim_at DATETIME")
    if "champion_lock_until" not in existing_columns:
        conn.exec_driver_sql("ALTER TABLE settings ADD COLUMN champion_lock_until DATETIME")

    existing_celeb_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(celebrities)").fetchall()}
    if "is_round_champion" not in existing_celeb_columns:
        conn.exec_driver_sql("ALTER TABLE celebrities ADD COLUMN is_round_champion BOOLEAN DEFAULT 0")

def get_or_create_settings(sess: Session) -> Settings:
    s = sess.get(Settings, 1)
    if not s:
        s = Settings(id=1, next_amount_cents=MIN_CHARGE_CENTS)
        sess.add(s)
        sess.commit()
        sess.refresh(s)
        return s

    dirty = False

    if s.next_amount_cents < MIN_CHARGE_CENTS:
        # Guards against a pre-existing DB (or a manual edit) leaving the
        # price below what Stripe will actually let anyone pay.
        s.next_amount_cents = MIN_CHARGE_CENTS
        dirty = True
    elif s.next_amount_cents > MAX_CHARGE_CENTS:
        # Same idea, but for the ceiling — defensive only; the webhook's own
        # ceiling logic (see below) should never let this happen on its own.
        s.next_amount_cents = MIN_CHARGE_CENTS
        dirty = True

    # Lazy inactivity reset: checked whenever settings are read (page loads,
    # checkout creation) rather than on a schedule/cron, since that needs no
    # background job infrastructure and is cheap — this only ever writes
    # when the condition is actually true, which should be rare.
    last_claim = as_utc(s.last_claim_at)
    if s.next_amount_cents > MIN_CHARGE_CENTS and last_claim is not None:
        if now_utc() - last_claim > timedelta(hours=INACTIVITY_RESET_HOURS):
            s.next_amount_cents = MIN_CHARGE_CENTS
            dirty = True

    if dirty:
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
    return {"current_year": now_utc().year, "site_url": SITE_URL}

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

# --- Social share image (Open Graph) ---
# Fonts are bundled in static/fonts/ (DejaVu, permissively licensed and
# redistributable) rather than relying on whatever fonts happen to be
# installed on the deploy host — Render's base image isn't guaranteed to
# have any, and this way the rendered image looks identical everywhere.
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")
_FONT_SERIF_BOLD = os.path.join(_FONT_DIR, "DejaVuSerif-Bold.ttf")
_FONT_SANS_BOLD = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")
_FONT_MONO_BOLD = os.path.join(_FONT_DIR, "DejaVuSansMono-Bold.ttf")

def generate_og_image(headline: str, eyebrow: str, subline: str) -> bytes:
    # Mirrors the site's dark navy / gold / pink look so a shared link's
    # preview card reads as the same product, not a generic placeholder.
    W, H = 1200, 630
    top, bottom = (10, 7, 20), (20, 11, 40)
    img = Image.new("RGB", (W, H), top)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        draw.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse((-250, -300, 550, 300), fill=(255, 62, 142, 120))
    gdraw.ellipse((750, -350, 1450, 250), fill=(139, 92, 246, 100))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(img)

    gold, text_color, muted, pink = (255, 201, 74), (241, 237, 249), (170, 160, 196), (255, 90, 130)

    draw.ellipse((70, 64, 118, 112), fill=pink)
    draw.text((94, 88), "¢", font=ImageFont.truetype(_FONT_SANS_BOLD, 26), fill=(26, 15, 17), anchor="mm")
    draw.text((134, 74), "PENNY CELEBRITY", font=ImageFont.truetype(_FONT_SANS_BOLD, 27), fill=muted)

    draw.text((70, 192), eyebrow.upper(), font=ImageFont.truetype(_FONT_SANS_BOLD, 25), fill=gold)

    max_width = W - 140
    size = 96
    name_font = ImageFont.truetype(_FONT_SERIF_BOLD, size)
    while draw.textlength(headline, font=name_font) > max_width and size > 44:
        size -= 4
        name_font = ImageFont.truetype(_FONT_SERIF_BOLD, size)
    draw.text((68, 236), headline, font=name_font, fill=gold)

    draw.text((70, 378), subline, font=ImageFont.truetype(_FONT_MONO_BOLD, 46), fill=text_color)

    draw.line([(70, H - 108), (W - 70, H - 108)], fill=(60, 48, 90), width=2)
    draw.text((70, H - 78), "pennycelebrity.com   •   one cent more than the last person",
               font=ImageFont.truetype(_FONT_SANS_BOLD, 24), fill=muted)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

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

        # Only meaningful if there's actually a raised price to fall back
        # from — no point counting down toward the floor when we're already
        # sitting at it (either nobody's claimed yet, or a ceiling/inactivity
        # reset already brought it back down).
        # A champion who paid the $20 ceiling is protected from being
        # dethroned until this passes (see CHAMPION_HOLD_MINUTES). Surface
        # it prominently and hide the less-relevant 48h inactivity hint
        # underneath it — 30 minutes will always resolve first.
        lock_until = as_utc(s.champion_lock_until)
        locked = lock_until is not None and now_utc() < lock_until

        reset_deadline = None
        last_claim = as_utc(s.last_claim_at)
        if not locked and s.next_amount_cents > MIN_CHARGE_CENTS and last_claim is not None:
            reset_deadline = last_claim + timedelta(hours=INACTIVITY_RESET_HOURS)

        return render_template("index.html",
                               publishable_key=STRIPE_PUBLISHABLE_KEY,
                               next_amount_cents=s.next_amount_cents,
                               current=current,
                               site_url=SITE_URL,
                               reset_deadline=reset_deadline,
                               locked=locked,
                               lock_deadline=lock_until if locked else None,
                               champion_hold_minutes=CHAMPION_HOLD_MINUTES,
                               # Paying the ceiling price now pays off: it
                               # locks in guaranteed spotlight time and a
                               # permanent Round Champion listing. Surface
                               # that BEFORE they pay, not after.
                               at_ceiling=(s.next_amount_cents == MAX_CHARGE_CENTS))

@app.get("/leaderboard")
def leaderboard():
    with Session(engine) as sess:
        # Oldest first, so that as we fold reigns together below, the last
        # write for a given identity is their most recent display name.
        celebs = sess.execute(
            select(Celebrity).order_by(Celebrity.start_time.asc())
        ).scalars().all()

        # A person can win, get dethroned, and win again later. Those are
        # separate rows in the DB (one per reign, which keeps the payment
        # history intact) but should read as ONE entry on the leaderboard
        # with their held time added together. There's no login system, so
        # "the same person" here just means "typed the same display name" —
        # two different people using an identical name will be folded
        # together too; that's an accepted limitation, not a bug.
        n = now_utc()
        grouped = {}
        for c in celebs:
            c.start_time = as_utc(c.start_time)
            c.end_time = as_utc(c.end_time)
            end = c.end_time or n
            duration_s = int((end - c.start_time).total_seconds())

            key = c.name.strip().casefold()
            g = grouped.get(key)
            if g is None:
                g = {
                    "name": c.name,
                    "total_seconds": 0,
                    "reigns": 0,
                    "total_paid_cents": 0,
                    "first_start": c.start_time,
                    "is_active": False,
                    "champion_reigns": 0,
                }
                grouped[key] = g

            g["name"] = c.name  # most recent casing/spelling wins for display
            g["total_seconds"] += duration_s
            g["reigns"] += 1
            g["total_paid_cents"] += c.amount_cents
            if c.is_round_champion:
                g["champion_reigns"] += 1
            if c.start_time < g["first_start"]:
                g["first_start"] = c.start_time
            if c.end_time is None:
                g["is_active"] = True

        top = sorted(grouped.values(), key=lambda g: g["total_seconds"], reverse=True)[:50]

        # Separate hall of fame: everyone who ever paid the $20 ceiling and
        # closed out a round, newest first. Distinct from "top" above, which
        # ranks by total time held — this ranks a different achievement.
        champions = sess.execute(
            select(Celebrity)
            .where(Celebrity.is_round_champion == True)  # noqa: E712
            .order_by(Celebrity.start_time.desc())
        ).scalars().all()
        for ch in champions:
            ch.start_time = as_utc(ch.start_time)

        return render_template("leaderboard.html", top=top, champions=champions[:50],
                               champion_hold_minutes=CHAMPION_HOLD_MINUTES)

@app.get("/og-image.png")
def og_image():
    # One dynamic image reused as the og:image for every page (see
    # layout.html), so ANY link to the site — homepage, leaderboard,
    # whatever — shows a live preview of who currently holds the spotlight
    # and what it costs to take it. That preview card is what actually
    # gets a shared link clicked on social platforms.
    with Session(engine) as sess:
        s = get_or_create_settings(sess)
        current = sess.execute(
            select(Celebrity).where(Celebrity.end_time.is_(None)).order_by(Celebrity.start_time.desc())
        ).scalars().first()
        lock_until = as_utc(s.champion_lock_until)
        locked = lock_until is not None and now_utc() < lock_until

        if current:
            headline = current.name
            if locked:
                eyebrow = "Protected Round Champion"
                subline = f"Paid {usd(current.amount_cents)} to win"
            else:
                eyebrow = "Currently holding the spotlight"
                subline = f"Steal it for {usd(s.next_amount_cents)}"
        else:
            headline = "No one yet"
            eyebrow = "Be the first Penny Celebrity"
            subline = f"Claim it for {usd(s.next_amount_cents)}"

    png_bytes = generate_og_image(headline, eyebrow, subline)
    resp = app.response_class(png_bytes, mimetype="image/png")
    # Short cache: keeps the preview fresh (this is a live leaderboard) but
    # avoids regenerating the image on every single crawler hit.
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp

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
        lock_until = as_utc(s.champion_lock_until)
        if lock_until and now_utc() < lock_until:
            # Reject before Stripe is even involved: the current champion
            # is under their guaranteed hold, so this payment would just
            # get refunded anyway (see /webhook). Blocking it here saves
            # the payer a pointless charge-then-refund round trip and the
            # site the Stripe processing fee on a doomed payment.
            remaining_min = max(1, int((lock_until - now_utc()).total_seconds() // 60) + 1)
            return jsonify(error=f"The current champion is protected for about {remaining_min} more "
                                  f"minute(s) — try again after that."), 409
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
    # Stripe redirects the browser here the instant Checkout completes —
    # that happens independently of, and often slightly before, our /webhook
    # actually deciding whether this payment won or lost the price race. So
    # this route can't just say "success"; it has to look up what actually
    # happened, or it risks telling a refunded loser they're the celebrity.
    session_id = request.args.get("session_id")
    outcome = "no_session"
    celeb = None

    if session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            payment_intent_id = checkout_session.payment_intent
        except Exception:
            payment_intent_id = None

        if not payment_intent_id:
            outcome = "no_session"
        else:
            outcome = "pending"
            # The webhook usually lands within a second, but there's no
            # ordering guarantee against this redirect, so poll briefly
            # rather than assume either outcome on the first check.
            for attempt in range(6):
                with Session(engine) as sess:
                    celeb = sess.execute(
                        select(Celebrity).where(Celebrity.stripe_payment_intent == payment_intent_id)
                    ).scalars().first()
                if celeb:
                    celeb.start_time = as_utc(celeb.start_time)
                    outcome = "won"
                    break
                try:
                    refunds = stripe.Refund.list(payment_intent=payment_intent_id, limit=1)
                    if refunds.data:
                        outcome = "refunded"
                        break
                except Exception:
                    pass
                if attempt < 5:
                    time.sleep(0.5)

    return render_template("success.html", outcome=outcome, celeb=celeb)

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

            lock_until = as_utc(s.champion_lock_until)
            if lock_until and now_utc() < lock_until:
                # Server-side backstop for the champion's guaranteed hold.
                # /create-checkout-session already blocks new attempts while
                # locked, but a Checkout session started just before the
                # lock began can still complete after it started — refund
                # it here rather than let it dethrone a protected champion.
                stripe.Refund.create(
                    payment_intent=payment_intent_id,
                    idempotency_key=f"refund-protected-{payment_intent_id}",
                )
                return "refunded - champion protected", 200

            if amount_total != current_required:
                # Amount mismatch -> refund. The idempotency key means a
                # redelivered webhook for the same payment intent can't
                # trigger a second refund attempt.
                stripe.Refund.create(
                    payment_intent=payment_intent_id,
                    idempotency_key=f"refund-mismatch-{payment_intent_id}",
                )
                return "refunded due to race", 200

            # If this claim hits the ceiling, the ladder resets back to the
            # floor for whoever claims next, instead of climbing forever —
            # and this payer becomes a Round Champion with a guaranteed hold.
            is_ceiling_win = (current_required == MAX_CHARGE_CENTS)
            next_price = current_required + 1
            if next_price > MAX_CHARGE_CENTS:
                next_price = MIN_CHARGE_CENTS
            new_lock_until = (now_utc() + timedelta(minutes=CHAMPION_HOLD_MINUTES)) if is_ceiling_win else None

            # Atomic compare-and-swap on the price itself: the UPDATE only
            # matches (and only takes effect) if next_amount_cents is still
            # what we just read. If a concurrent webhook already bumped it,
            # rowcount is 0 and we know we lost the race, without needing
            # row-level locking that SQLite doesn't support. last_claim_at
            # and champion_lock_until are stamped in the same statement so
            # everything resets atomically with the price, with no extra
            # round trip.
            result = sess.execute(
                update(Settings)
                .where(Settings.id == 1, Settings.next_amount_cents == current_required)
                .values(next_amount_cents=next_price, last_claim_at=now_utc(),
                        champion_lock_until=new_lock_until)
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
                status="succeeded",
                is_round_champion=is_ceiling_win,
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
