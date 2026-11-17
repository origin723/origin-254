"""Create a compressed PostgreSQL backup and prune old ones.

    python manage.py backup_db                 # writes backups/origin254-YYYYmmdd-HHMMSS.dump
    python manage.py backup_db --keep-days 30

Schedule it (cron, launchd, a Kubernetes CronJob or your host's scheduler), e.g.:
    15 2 * * *  cd /srv/origin254 && .venv/bin/python manage.py backup_db
Restore with:
    pg_restore --clean --if-exists -d "$DATABASE_URL" backups/origin254-....dump
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from apps.core.audit import log_action


class Command(BaseCommand):
    help = "Back up the database with pg_dump (custom format) and delete backups older than the retention period."

    def add_arguments(self, parser):
        parser.add_argument("--keep-days", type=int, default=settings.BACKUP_RETENTION_DAYS)
        parser.add_argument("--dir", default=str(settings.BACKUP_DIR))

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise CommandError("backup_db supports PostgreSQL only.")
        pg_dump = shutil.which("pg_dump")
        if not pg_dump:
            raise CommandError("pg_dump not found on PATH.")
        db = settings.DATABASES["default"]
        out_dir = Path(opts["dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"origin254-{timezone.localtime():%Y%m%d-%H%M%S}.dump"
        env = {**os.environ, "PGPASSWORD": db.get("PASSWORD", "")}
        cmd = [pg_dump, "--format=custom", "--no-owner", f"--file={target}", f"--dbname={db['NAME']}"]
        for flag, key in (("--host", "HOST"), ("--port", "PORT"), ("--username", "USER")):
            if db.get(key):
                cmd.append(f"{flag}={db[key]}")
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(f"pg_dump failed: {result.stderr.strip()}")
        size_kb = target.stat().st_size // 1024
        self.stdout.write(self.style.SUCCESS(f"Backup written: {target} ({size_kb} KB)"))

        cutoff = time.time() - opts["keep_days"] * 86400
        removed = 0
        for old in out_dir.glob("origin254-*.dump"):
            if old.stat().st_mtime < cutoff:
                old.unlink()
                removed += 1
        if removed:
            self.stdout.write(f"Removed {removed} backup(s) older than {opts['keep_days']} days.")
        log_action(None, "system.backup", None, file=target.name, size_kb=size_kb, pruned=removed)
