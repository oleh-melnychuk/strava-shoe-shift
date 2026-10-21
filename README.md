# Strava Shoe Shift

Telegram bot that watches new Strava runs, uses AI to classify the workout type
(long / tempo / intervals / recovery / race), picks a shoe from your Strava gear
and usage history, updates the activity, and asks you in Telegram when unsure.

## Features

- Strava OAuth via `/link` in Telegram
- Strava `activity.create` webhook pipeline
- Gemini recommendation with heuristic fallback if no API key
- Confidence threshold (default `0.75`) — auto-assign or inline confirmation
- Learned preferences from your accept/reject choices

## Setup

### 1. Create credentials

1. **Telegram:** talk to [@BotFather](https://t.me/BotFather), create a bot, copy the token.
2. **Strava:** create an app at [developers.strava.com](https://www.strava.com/settings/api).
   - Authorization Callback Domain must match your host (e.g. `localhost` or your tunnel domain).
   - Set redirect URI to `{PUBLIC_BASE_URL}/oauth/strava/callback`.
3. **Gemini:** create an API key at [Google AI Studio](https://aistudio.google.com/apikey) (optional for heuristic-only mode).

### 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your tokens
```

Start Redis (required):

```bash
docker compose up -d redis
# or: brew services start redis
```

### 3. Run locally

```bash
uvicorn app.main:app --reload --port 8000
```

With `TELEGRAM_POLLING=true` (default), the bot receives Telegram updates without a public URL.

Strava webhooks **require** a public HTTPS URL. Use a tunnel (ngrok, Cloudflare Tunnel, etc.):

```bash
# example
ngrok http 8000
# set PUBLIC_BASE_URL and STRAVA_REDIRECT_URI to the https URL, restart the app
python scripts/create_strava_webhook.py create
python scripts/create_strava_webhook.py view
```

### 4. Use the bot

1. Message the bot `/start` then `/link`
2. Complete Strava OAuth in the browser
3. Upload a run (or create an activity) on Strava
4. Bot assigns shoes or asks you to confirm

Useful commands: `/shoes`, `/recalc`, `/prefer`, `/prefs`, `/status`

- `/recalc` — list recent runs and re-run AI shoe assignment
- Assignment uses your **last 20 Strava runs** (pace/HR/name + shoes), not stored prefs
- `/prefs clear` — wipe old prefs if they were wrong
- `/prefer` — optional manual pin only (history remains primary)

## Configuration

See [`.env.example`](.env.example).

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` | Strava API app |
| `STRAVA_VERIFY_TOKEN` | Shared secret for webhook subscription |
| `STRAVA_REDIRECT_URI` | Must match OAuth callback |
| `GEMINI_API_KEY` | Gemini AI recommendations |
| `GEMINI_MODEL` | Primary model (default `gemini-3.5-flash`) |
| `GEMINI_FALLBACK_MODEL` | Backup model on 429/503 (default `gemini-3-flash-preview`) |
| `PUBLIC_BASE_URL` | Public base URL for webhooks/OAuth |
| `REDIS_URL` | Redis connection URL (default `redis://localhost:6379/0`) |
| `CONFIDENCE_THRESHOLD` | Auto-assign cutoff (0–1) |
| `TELEGRAM_POLLING` | `true` locally; `false` in production with webhook |

## Storage (Redis)

All app state lives in Redis (no SQLite/Postgres):

| Key pattern | Data |
| --- | --- |
| `user:tg:{telegram_id}` | Linked user + Strava tokens |
| `idx:strava:{athlete_id}` | Lookup Telegram user by Strava athlete |
| `processed:{activity_id}` | Processed activity record |
| `pending:{id}` | Confirmation prompt (24h TTL) |
| `pref:{user}:{run_type}:{shoe_id}` | Learned shoe preferences |

## Production

- Set `TELEGRAM_POLLING=false`
- Expose the app on HTTPS
- Telegram webhook: `{PUBLIC_BASE_URL}/telegram/webhook`
- Strava webhook: `{PUBLIC_BASE_URL}/strava/webhook`
- Or run via Docker:

```bash
docker compose up --build
```

## Tests

```bash
pytest
```

Smoke tests cover run-type heuristics, shoe payload/usage aggregation, and AI fallback without calling Gemini/Strava.

## Project layout

```
app/
  main.py              # FastAPI + lifespan
  config.py
  models.py / db.py / store.py   # Redis models + repository
  strava/              # OAuth, API client, webhook
  telegram/            # commands + confirmation callbacks
  ai/                  # classifier + recommender
  services/            # assign_shoe orchestration
scripts/create_strava_webhook.py
tests/
```
