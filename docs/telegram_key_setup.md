# Telegram Key Setup Guide

## 1. Create Bot Token (BotFather)
1. Open Telegram and search for `@BotFather`.
2. Send `/newbot`.
3. Enter bot name and username.
4. BotFather returns a token like:
   `123456789:AA...`
5. Put this value into `TELEGRAM_BOT_TOKEN` in your `.env` file.

## 2. Create Webhook Secret
Use any long random string. Example command:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put result into `TELEGRAM_WEBHOOK_SECRET`.

## 3. Public Base URL
Your FastAPI webhook endpoint must be reachable from Telegram.
Set `TELEGRAM_PUBLIC_BASE_URL` to your public URL, for example:
- `https://your-domain.com`
- `https://xxxx.ngrok-free.app`

## 4. Admin API Key
Generate one random key for admin endpoints:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put result into `ADMIN_API_KEY`.

## 5. Fill data paths
- `SQLITE_DB_PATH`: local DB file path
- `CARDS_JSON_PATH`: path to cards.json
- `ASSETS_BASE_DIR`: root directory that contains cards/ and headers/

Keep absolute paths to avoid path confusion.

## 6. Set webhook
After app is running, call:

```bash
POST /admin/telegram/set-webhook
Header: X-Admin-Key: <ADMIN_API_KEY>
```

Example with curl:

```bash
curl -X POST "http://localhost:8000/admin/telegram/set-webhook" \
  -H "X-Admin-Key: YOUR_ADMIN_API_KEY"
```
