"""Copy uploaded files from local PRIVATE_MEDIA_ROOT into the configured default storage.

Use once after switching to object storage (S3_BUCKET set):
    python manage.py upload_media --dry-run
    python manage.py upload_media
Files already present in the bucket are skipped, so it is safe to re-run.
"""
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Upload files from PRIVATE_MEDIA_ROOT to the default (object) storage, keeping their names."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=str(settings.PRIVATE_MEDIA_ROOT), help="Local folder to upload")
        parser.add_argument("--dry-run", action="store_true", help="List what would be uploaded")

    def handle(self, *args, source, dry_run, **opts):
        root = Path(source)
        if not root.is_dir():
            raise CommandError(f"{root} is not a directory")
        backend = settings.STORAGES["default"]["BACKEND"]
        if backend.endswith("FileSystemStorage"):
            raise CommandError("Default storage is the local filesystem; set S3_BUCKET (and credentials) first.")

        uploaded = skipped = 0
        for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")):
            name = path.relative_to(root).as_posix()
            if default_storage.exists(name):
                skipped += 1
                continue
            if dry_run:
                self.stdout.write(f"would upload {name}")
            else:
                with path.open("rb") as fh:
                    saved = default_storage.save(name, File(fh))
                if saved != name:
                    raise CommandError(f"{name} was stored as {saved}; database references would break")
                self.stdout.write(f"uploaded {name}")
            uploaded += 1
        verb = "would upload" if dry_run else "uploaded"
        self.stdout.write(self.style.SUCCESS(f"{verb} {uploaded} file(s), {skipped} already present"))
