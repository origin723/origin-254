import logging

from .middleware import client_ip, get_current_request
from .models import AuditLog

logger = logging.getLogger("origin254.audit")


def log_action(actor, action, target=None, **details):
    """Record a sensitive action. `actor` may be None for system actions."""
    request = get_current_request()
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    entry = AuditLog.objects.create(
        actor=actor,
        action=action,
        target_type=target._meta.label if target is not None else "",
        target_id=str(target.pk) if target is not None else "",
        target_repr=str(target)[:255] if target is not None else "",
        details={k: (v if isinstance(v, (int, float, bool, type(None), list, dict)) else str(v))
                 for k, v in details.items()},
        ip_address=client_ip(request),
    )
    logger.info("%s by %s on %s", action, actor, entry.target_repr)
    return entry
