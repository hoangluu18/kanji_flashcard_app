import secrets; 
print('ADMIN_API_KEY=' + secrets.token_urlsafe(48));
print('TELEGRAM_WEBHOOK_SECRET=' + secrets.token_urlsafe(48))