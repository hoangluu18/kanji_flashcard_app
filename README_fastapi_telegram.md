# FastAPI + Telegram Kanji SRS Bot

## What this implementation includes
- FastAPI server with webhook endpoint.
- Telegram bot using `python-telegram-bot` (async mode).
- SQLite local database with SRS state.
- Scheduler (APScheduler) for morning/noon/evening reminders.
- File-id caching for Telegram image sends.
- Error handling and admin endpoints.

## Project files
- `app/main.py`: FastAPI app and endpoints.
- `app/telegram_service.py`: Telegram command/callback handlers.
- `app/scheduler_service.py`: scheduled reminder jobs.
- `app/srs.py`: SM-2 style review algorithm.
- `app/models.py`: SQLite schema (SQLAlchemy).
- `app/catalog.py`: load and seed kanji/card catalog from cards.json.
- `.env.example`: all required config aliases.
- `docs/telegram_key_setup.md`: key and webhook setup guide.

## Quick start
1. Create virtual environment and install deps:

```bash
pip install -r requirements.txt
```

2. Create `.env` from `.env.example` and replace all `ALIAS_*` values.

3. Run API:

```bash
python run_api.py
```

4. Verify service:
- `GET /health`
- `GET /health/deep`

5. Set webhook (after public URL is available):
- `POST /admin/telegram/set-webhook`
- header: `X-Admin-Key: <your key>`

## Important notes
- The app skips catalog seeding if `CARDS_JSON_PATH` or `ASSETS_BASE_DIR` still contains `ALIAS_`.
- Telegram bot is disabled until `TELEGRAM_BOT_TOKEN` is set.
- Scheduler is also disabled when bot is disabled.
- If `TELEGRAM_USE_WEBHOOK=true`, webhook is auto-ensured on startup.
- If `TELEGRAM_USE_WEBHOOK=false`, bot runs in polling mode and does not need public URL/webhook.

## Telegram mode switch
- Webhook mode (production-like):
	- `TELEGRAM_USE_WEBHOOK=true`
	- Requires valid `TELEGRAM_PUBLIC_BASE_URL` and `TELEGRAM_WEBHOOK_SECRET`
	- Startup will auto call Telegram `setWebhook`
- Polling mode (local dev):
	- `TELEGRAM_USE_WEBHOOK=false`
	- Bot receives updates via long polling
	- Existing webhook is removed automatically on startup

## Optional manual jobs
You can run jobs manually:
- `POST /admin/jobs/run/morning`
- `POST /admin/jobs/run/noon`
- `POST /admin/jobs/run/evening`
- `POST /admin/jobs/run/maintenance`
