# Origin 254 production image. Build context is this backend/ folder:
#   docker build backend/          (PrimeCloud: Build Root Directory = backend)
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    PRIVATE_MEDIA_ROOT=/app/private_media \
    BACKUP_DIR=/app/backups

# postgresql-client provides pg_dump for the backup_db command
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Code lives in /app/backend so settings' PROJECT_ROOT is /app (static, media, backups).
WORKDIR /app/backend
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN DJANGO_SECRET_KEY=build DJANGO_DEBUG=0 python manage.py collectstatic --noinput \
    && chmod +x start.sh \
    && useradd --create-home --uid 1000 app \
    && mkdir -p "$PRIVATE_MEDIA_ROOT" "$BACKUP_DIR" && chown -R app /app
USER app

EXPOSE 8000
CMD ["./start.sh"]
