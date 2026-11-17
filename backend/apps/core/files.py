"""Opening stored uploads safely (local disk or S3/R2 object storage)."""
import logging

from django.http import Http404

logger = logging.getLogger("origin254.files")


def open_stored(field_file):
    """Open a FileField's file for reading, or raise Http404 if it can't be read.

    A missing object or an unreachable/misconfigured bucket is logged (so it
    shows in platform logs) instead of turning into a 500 for the user.
    """
    if not field_file:
        raise Http404
    try:
        return field_file.open("rb")
    except Exception as exc:  # FileNotFoundError locally; botocore ClientError etc. on S3
        logger.error("Could not open stored file %r: %s", field_file.name, exc)
        raise Http404("File not available") from exc


STORAGE_UNAVAILABLE_MESSAGE = "File storage is temporarily unavailable, so the file could not be saved. Please try again later."


def is_storage_error(exc):
    """True for errors raised by the S3/R2 client (bad credentials, network, service errors)."""
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:  # object storage not installed
        return False
    return isinstance(exc, (BotoCoreError, ClientError))
