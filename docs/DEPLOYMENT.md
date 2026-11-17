# Deployment

## Option A: Docker Compose (single server, PostGIS included)

```bash
cp .env.example .env
# Edit .env: set DJANGO_SECRET_KEY, DJANGO_ALLOWED_HOSTS, DJANGO_CSRF_TRUSTED_ORIGINS,
# EMAIL_* and SMS/WhatsApp credentials. Choose a strong POSTGRES_PASSWORD in your shell or a .env file.
export POSTGRES_PASSWORD='a-long-random-password'
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_demo        # optional: demo data
```

Services:
- **db** runs `postgis/postgis:16-3.4`. Migration `core/0002_postgis` enables the extension and the spatial index automatically.
- **web** runs gunicorn on port 8000. Migrations run on start, and whitenoise serves static files.
- **backup** runs `backup_db` every 24 hours and keeps 14 days of dumps in the `backups` volume.

Put a TLS-terminating reverse proxy (Caddy, nginx, or a cloud load balancer) in front of port 8000. Then set `DJANGO_SECURE_SSL_REDIRECT=1`.

## Option B: any Linux host or PaaS

1. Python 3.13, then `pip install -r requirements.txt`.
2. PostgreSQL 14+ with PostGIS: `apt install postgresql-16-postgis-3`, or a managed database with PostGIS enabled (AWS RDS, Azure, GCP Cloud SQL and DigitalOcean all support it).
3. Set the environment variables (table below), then run:
   ```bash
   python manage.py migrate
   python manage.py collectstatic --noinput
   gunicorn origin254.wsgi:application --bind 0.0.0.0:8000 --workers 3
   ```
4. Point a process manager (systemd, supervisord, or your PaaS) at the gunicorn command, with `/health/` as the health check.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `DJANGO_SECRET_KEY` | **Required in production.** 50+ random characters | dev key |
| `DJANGO_DEBUG` | `0` in production (enables secure cookies, HSTS, SSL redirect) | `0` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated host names | `localhost,127.0.0.1` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | e.g. `https://app.origin254.co.ke` | |
| `DJANGO_SECURE_SSL_REDIRECT` | Redirect HTTP to HTTPS | `1` when not in debug |
| `DATABASE_URL` | `postgres://user:pass@host:5432/origin254` | local `origin254` |
| `PRIVATE_MEDIA_ROOT` | Directory for verification documents, inspection reports and PODs. **Must not be web-served.** Back it up | `./private_media` |
| `CORS_ALLOWED_ORIGINS` | Origins of separate frontends | |
| `NOTIFICATION_CHANNELS` | Subset of `in_app,email,sms,whatsapp` | `in_app,email` |
| `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | SMTP settings | console |
| `SMS_PROVIDER` | `console`, `africastalking` or `twilio` | `console` |
| `AFRICASTALKING_USERNAME`, `AFRICASTALKING_API_KEY` | Africa's Talking SMS | |
| `WHATSAPP_PROVIDER` | `console` or `twilio` | `console` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_SMS_FROM`, `TWILIO_WHATSAPP_FROM` | Twilio SMS and WhatsApp | |
| `REQUIRE_ACCOUNT_VERIFICATION` | `1` makes new accounts wait for admin approval; `0` approves them at registration | `0` |
| `MIN_PHOTOS_PER_LISTING` | Photos a stock listing needs before buyers can see it | `2` |
| `INVENTORY_STALE_DAYS` | Days before unconfirmed stock is excluded from matching | `14` |
| `BACKUP_DIR`, `BACKUP_RETENTION_DAYS` | Backup location and retention | `./backups`, `14` |
| `LOG_LEVEL` | Application log level | `INFO` |

`EMAIL_BACKEND` defaults to the console backend. For real email, set it to `django.core.mail.backends.smtp.EmailBackend` and fill in `EMAIL_HOST`, `EMAIL_PORT` (587), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and `EMAIL_USE_TLS` (on).

## Payments (M-Pesa)

Buyers pay an order with an M-Pesa prompt on their phone (Daraja STK Push). Origin 254 holds the money and pays each supplier when the buyer confirms receipt of that supplier's lot.

| Variable | Meaning |
|---|---|
| `PAYMENT_PROVIDER` | `simulated` (default, test mode: payments succeed instantly, no money moves) or `mpesa` |
| `PAYOUT_PROVIDER` | `manual` (default: an admin pays the supplier and records the transaction code under **Payments & payouts**), `simulated`, or `mpesa` (automatic B2C) |
| `PLATFORM_FEE_PERCENT` | Deducted from each supplier payout (default 0) |
| `REQUIRE_PAYMENT_BEFORE_DISPATCH` | `1` blocks pickup until the order is paid |
| `MPESA_ENVIRONMENT` | `sandbox` or `production` |
| `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET` | From your app on developer.safaricom.co.ke |
| `MPESA_SHORTCODE`, `MPESA_PASSKEY` | Lipa na M-Pesa Online shortcode and passkey (sandbox: 174379 and the sandbox passkey) |
| `MPESA_TRANSACTION_TYPE`, `MPESA_TILL_OR_PAYBILL` | `CustomerPayBillOnline` for a paybill; `CustomerBuyGoodsOnline` plus the till number for a till |
| `MPESA_CALLBACK_BASE_URL` | Public HTTPS address of the site, e.g. `https://origin254.example.com` |
| `MPESA_CALLBACK_TOKEN` | Long random secret placed in the callback URL (Safaricom does not sign callbacks) |
| `MPESA_B2C_SHORTCODE`, `MPESA_B2C_INITIATOR`, `MPESA_B2C_SECURITY_CREDENTIAL` | Only for automatic payouts (`PAYOUT_PROVIDER=mpesa`) |

Safaricom calls these URLs, which must be reachable from the internet:

- `POST {MPESA_CALLBACK_BASE_URL}/payments/mpesa/stk/{MPESA_CALLBACK_TOKEN}/`: buyer payment result
- `POST .../payments/mpesa/b2c-result/{token}/` and `.../b2c-timeout/{token}/`: payout results

When developing on your own computer, Safaricom cannot reach `127.0.0.1`. Use a tunnel such as ngrok for `MPESA_CALLBACK_BASE_URL`, or tap **Check payment status** on the order page, which asks M-Pesa directly. Successful payments are checked against the expected amount, and each M-Pesa code can only be used once.

## Backups

```bash
python manage.py backup_db                 # pg_dump custom format → $BACKUP_DIR/origin254-YYYYmmdd-HHMMSS.dump
python manage.py backup_db --keep-days 30  # change retention
pg_restore --clean --if-exists -d "$DATABASE_URL" backups/origin254-20261117-021500.dump   # restore
```

Schedule it with cron, for example `15 2 * * * cd /srv/origin254 && .venv/bin/python manage.py backup_db`, or use your platform's scheduler. Copy the backup directory and `PRIVATE_MEDIA_ROOT` off the server (object storage such as S3). Managed databases add point-in-time recovery on top. Each backup run is recorded in the audit log.

## Production checklist

- [ ] `DJANGO_DEBUG=0` and a unique `DJANGO_SECRET_KEY`
- [ ] HTTPS in front, with `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` set
- [ ] PostGIS enabled (check: `SELECT postgis_version();`)
- [ ] An admin created with `createsuperuser`, and the catalogue and locations loaded
- [ ] SMTP and an SMS provider configured; `NOTIFICATION_CHANNELS` set
- [ ] Daily backups scheduled and a restore tested; private media backed up
- [ ] `python manage.py check --deploy` passes
- [ ] Logs shipped (stdout) and `/health/` monitored

## Scaling notes

- The app is stateless apart from `PRIVATE_MEDIA_ROOT`. To run several instances, move files to S3-compatible storage (`django-storages`, signed URLs) and run more gunicorn workers or containers behind the load balancer.
- Move notification delivery (`comms.notify._deliver`) to a task queue (Celery or RQ with Redis) when SMS or email volume grows.
- Matching is currently synchronous and fast at MVP scale: a few SQL queries, plus Python scoring over at most a few hundred listings. At national scale, run it in the task queue and keep the PostGIS index.
