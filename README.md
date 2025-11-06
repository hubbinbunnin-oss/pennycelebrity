# PennyCelebrity — Flask + Stripe MVP

This is a production-ready MVP for **pennycelebrity.com**.

**Concept**
- Whoever most recently pays the required amount becomes the **Penny Celebrity** shown on the homepage.
- The required amount starts at **$0.01** and increases by **$0.01** every successful claim.
- Users can set any display name (with simple automatic moderation).
- A **leaderboard** shows who held the spot the longest.
- Stripe Checkout handles payments. A secure **webhook** finalizes the winner and increments the price.
- If two people pay at the same time for the same amount, only one wins; the other is **automatically refunded**.

---

## Quick Start (Local)

### 1) Requirements
- Python 3.10+
- A Stripe account (free) with test keys

### 2) Create and fill `.env`
Copy the example:
```bash
cp .env.example .env
```
Edit `.env` and set your keys:
- `STRIPE_SECRET_KEY`: Your Stripe secret key (e.g. `sk_test_...`)
- `STRIPE_PUBLISHABLE_KEY`: Your Stripe publishable key (e.g. `pk_test_...`)
- `STRIPE_WEBHOOK_SECRET`: From your Stripe CLI or Dashboard when you set up the webhook.
- `SITE_URL`: Your public URL (e.g. `http://localhost:5000` for local testing, or production domain).

### 3) Install & Run
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
flask --app app run --debug
```

### 4) Stripe Webhook (Local Testing)
Install Stripe CLI and run:
```bash
stripe listen --forward-to localhost:5000/webhook
```
Copy the webhook signing secret from the CLI output into `.env` as `STRIPE_WEBHOOK_SECRET`.

---

## Deploy (Render/Vercel/Heroku-like)

- **Render** (free tier works):
  1. Create a new **Web Service**, connect your repo.
  2. Runtime: **Python 3.10+**
  3. Build Command: `pip install -r requirements.txt`
  4. Start Command: `gunicorn -w 2 -k gthread -t 120 -b 0.0.0.0:10000 app:app`
  5. Add **Environment Variables** from `.env`.
  6. Add a **Stripe webhook** pointing to `https://YOUR_URL/webhook`.

- **Other hosts**: Any place that runs Flask + WSGI will work. Use `gunicorn` command above or similar.

---

## Files Overview

- `app.py` — Flask app, routes, Stripe integration, webhook, DB models.
- `requirements.txt` — Python dependencies.
- `templates/` — HTML templates (Jinja2).
- `static/style.css` — Simple styling.
- `moderation.py` — Very basic profanity & character filter.
- `Procfile` — Example process file for Heroku-ish platforms.
- `.env.example` — Example environment variables.

---

## Security & Concurrency

- The **webhook** confirms payment and performs a **compare-and-swap** on the current price in a DB transaction.
- If a paid amount **doesn't match** the current required price, the payment is **refunded automatically**.
- Simple name moderation blocks obvious slurs/obscene words and limits length.
- All dynamic Stripe amounts are created **server-side** to prevent tampering.

---

## Leaderboard Logic

- Each winner row has `start_time` and (once replaced) `end_time`.
- The leaderboard sorts by **duration = (end_time or now) - start_time**.
- The homepage shows the current celeb, the amount to dethrone, and a live-updating timer.

---

## Customization

- Styling is intentionally minimal — customize `static/style.css` and templates as you like.
- Add admin tools later (e.g., manual ban list) by checking an admin token from env vars.

---

## Legal / Terms Hint
Add a footer link to basic Terms/Content Rules explaining that obscene or hateful content will be removed without refund. (Currently, names with blocked terms are censored during submission, but explicit ToS is always good.)

Enjoy!
