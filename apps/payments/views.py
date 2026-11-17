"""Safaricom Daraja callbacks. Authenticated by the secret token in the URL (Daraja does not sign requests)."""
import hmac
import json
import logging

from django.conf import settings
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import services

logger = logging.getLogger("origin254.payments")
ACCEPTED = {"ResultCode": 0, "ResultDesc": "Accepted"}


def _check(token):
    expected = settings.MPESA_CALLBACK_TOKEN
    if not expected or not hmac.compare_digest(str(token), expected):
        raise Http404


def _body(request):
    try:
        return json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}


@csrf_exempt
@require_POST
def stk_callback(request, token):
    _check(token)
    body = _body(request)
    payment = services.handle_stk_callback(body)
    if payment is None:
        logger.warning("STK callback for unknown CheckoutRequestID: %s", str(body)[:300])
    return JsonResponse(ACCEPTED)


@csrf_exempt
@require_POST
def b2c_result(request, token):
    _check(token)
    services.handle_b2c_result(_body(request))
    return JsonResponse(ACCEPTED)


@csrf_exempt
@require_POST
def b2c_timeout(request, token):
    _check(token)
    services.handle_b2c_timeout(_body(request))
    return JsonResponse(ACCEPTED)
