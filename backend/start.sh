#!/bin/sh
# Container entrypoint: optional migrations, then gunicorn on 0.0.0.0:$PORT.
set -e

# On PrimeCloud run migrations as the Pre-Deploy Command instead
# (python manage.py migrate --noinput). Set MIGRATE_ON_START=1 to run them here.
if [ "${MIGRATE_ON_START:-0}" = "1" ]; then
    python manage.py migrate --noinput
fi

if [ ! -w "$PRIVATE_MEDIA_ROOT" ]; then
    echo "WARNING: PRIVATE_MEDIA_ROOT ($PRIVATE_MEDIA_ROOT) is not writable by uid $(id -u); uploads will fail." >&2
fi

# exec so gunicorn is PID 1 and receives SIGTERM (graceful timeout < platform's 10s).
exec gunicorn origin254.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${GUNICORN_WORKERS:-2}" \
    --timeout "${GUNICORN_TIMEOUT:-60}" \
    --graceful-timeout 8 \
    --access-logfile -
