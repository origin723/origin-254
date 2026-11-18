from decimal import Decimal

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()

STATUS_TONE = {
    "approved": "ok", "completed": "ok", "accepted": "ok", "passed": "ok", "delivered": "ok", "committed": "ok",
    "availability_confirmed": "ok", "supply_confirmed": "ok", "confirmed": "ok", "inspection_passed": "ok",
    "resolved": "ok",
    "pending": "warn", "open": "warn", "submitted": "warn", "countered": "warn", "contacted": "warn",
    "awaiting_reconfirmation": "warn", "pending_confirmation": "warn", "negotiating": "warn", "matched": "info",
    "potential": "info", "in_progress": "info", "in_transit": "info", "in_fulfilment": "info", "scheduled": "info",
    "picked_up": "info", "conditional": "warn", "under_review": "warn",
    "rejected": "bad", "failed": "bad", "cancelled": "bad", "declined": "bad", "suspended": "bad",
    "inspection_failed": "bad", "disputed": "bad", "withdrawn": "bad", "expired": "bad",
}


@register.filter
def tone(status):
    return STATUS_TONE.get(str(status), "muted")


@register.inclusion_tag("web/_badge.html")
def badge(obj, field="status"):
    value = getattr(obj, field)
    display = getattr(obj, f"get_{field}_display", lambda: value)()
    return {"value": value, "display": display, "tone": STATUS_TONE.get(str(value), "muted")}


@register.filter
def pct(value):
    if value in (None, ""):
        return "—"
    return f"{float(value) * 100:.0f}%"


@register.filter
def kes(value):
    if value in (None, ""):
        return "—"
    return f"KES {Decimal(value):,.0f}"


@register.filter
def qty(value):
    if value in (None, ""):
        return "—"
    v = Decimal(value)
    return f"{v:,.0f}" if v == v.to_integral() else f"{v:,.2f}"


@register.filter
def get(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def bar(value):
    """0..1 float -> CSS width percentage."""
    try:
        return max(0, min(100, round(float(value) * 100)))
    except (TypeError, ValueError):
        return 0


@register.filter
def isodate(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


@register.filter
def label(value):
    """min_size_mm -> Min size mm; delivery_picked_up -> Delivery picked up."""
    return str(value).replace("_", " ").capitalize()


_ASSET_VERSIONS = {}


@register.simple_tag
def asset_url(path):
    """Static URL with ?v=<content hash>, so browsers and CDNs refetch a file whenever it changes
    (also when DEBUG serves un-hashed file names)."""
    if path not in _ASSET_VERSIONS:
        import hashlib

        found = finders.find(path)
        _ASSET_VERSIONS[path] = hashlib.sha256(open(found, "rb").read()).hexdigest()[:10] if found else ""
    url, version = static(path), _ASSET_VERSIONS[path]
    return f"{url}?v={version}" if version else url
