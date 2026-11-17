"""Notification dispatch.

Every notification is stored in-app. It is additionally sent over the channels
enabled in settings.NOTIFICATION_CHANNELS that the user can receive
(email address, phone + SMS opt-in, phone + WhatsApp opt-in).

SMS and WhatsApp providers are pluggable: `console` (log only, default),
`africastalking` and `twilio`. Provider failures never break the business
transaction that triggered the notification.
"""
import logging
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction

from .models import Notification

logger = logging.getLogger("origin254.notify")


def _http_post(url, data, headers):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed provider URLs
        return resp.status


def send_sms(phone, text):
    provider = settings.SMS_PROVIDER
    if provider == "africastalking":
        return _http_post(
            "https://api.africastalking.com/version1/messaging",
            {"username": settings.AFRICASTALKING_USERNAME, "to": phone, "message": text},
            {"apiKey": settings.AFRICASTALKING_API_KEY, "Accept": "application/json"},
        )
    if provider == "twilio":
        import base64
        token = base64.b64encode(f"{settings.TWILIO_ACCOUNT_SID}:{settings.TWILIO_AUTH_TOKEN}".encode()).decode()
        return _http_post(
            f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json",
            {"To": phone, "From": settings.TWILIO_SMS_FROM, "Body": text},
            {"Authorization": f"Basic {token}"},
        )
    logger.info("[SMS:console] to=%s %s", phone, text)
    return None


def send_whatsapp(phone, text):
    if settings.WHATSAPP_PROVIDER == "twilio":
        import base64
        token = base64.b64encode(f"{settings.TWILIO_ACCOUNT_SID}:{settings.TWILIO_AUTH_TOKEN}".encode()).decode()
        return _http_post(
            f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json",
            {"To": f"whatsapp:{phone}", "From": settings.TWILIO_WHATSAPP_FROM, "Body": text},
            {"Authorization": f"Basic {token}"},
        )
    logger.info("[WhatsApp:console] to=%s %s", phone, text)
    return None


def _deliver(notification_id):
    n = Notification.objects.select_related("user").get(pk=notification_id)
    user, sent = n.user, ["in_app"]
    channels = settings.NOTIFICATION_CHANNELS
    text = f"Origin 254: {n.title}" + (f" — {n.body}" if n.body else "")
    attempts = [
        ("email", "email" in channels and user.email,
         lambda: send_mail(n.title, f"{n.body}\n\n{n.link}", settings.DEFAULT_FROM_EMAIL, [user.email])),
        ("sms", "sms" in channels and user.phone and user.sms_opt_in, lambda: send_sms(user.phone, text[:300])),
        ("whatsapp", "whatsapp" in channels and user.phone and user.whatsapp_opt_in,
         lambda: send_whatsapp(user.phone, text)),
    ]
    for name, enabled, fn in attempts:
        if not enabled:
            continue
        try:
            fn()
            sent.append(name)
        except Exception:  # pragma: no cover - provider/network failure
            logger.exception("Failed to send %s notification %s", name, n.pk)
    Notification.objects.filter(pk=n.pk).update(channels_sent=sent)


def notify(user, title, body="", kind=Notification.Kind.SYSTEM, link=""):
    n = Notification.objects.create(user=user, title=title, body=body, kind=kind, link=link)
    # Send external messages only once the surrounding transaction commits.
    transaction.on_commit(lambda: _deliver(n.pk))
    return n


def notify_admins(title, body="", kind=Notification.Kind.SYSTEM, link=""):
    from apps.accounts.models import Role, User
    for admin in User.objects.filter(role=Role.ADMIN, is_active=True):
        notify(admin, title, body, kind, link)

