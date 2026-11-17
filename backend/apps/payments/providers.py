"""Payment providers.

`simulated` (default): no money moves. Buyer payments succeed straight away with a fake
receipt, which is enough to run the full workflow locally and in tests.

`mpesa`: Safaricom Daraja API. STK Push (Lipa na M-Pesa Online) for buyer payments and
B2C for supplier payouts. Credentials come from settings / .env (MPESA_*). Safaricom
calls back to MPESA_CALLBACK_BASE_URL, which must be a public HTTPS address.
"""
import base64
import json
import logging
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("origin254.payments")

DARAJA_HOSTS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}


class ProviderError(Exception):
    pass


@dataclass
class PushResult:
    checkout_request_id: str
    merchant_request_id: str = ""
    message: str = ""
    # Simulated provider settles immediately
    settled: bool = False
    receipt_number: str = ""


@dataclass
class QueryResult:
    result_code: str  # "0" = paid, "" = still waiting
    description: str = ""


@dataclass
class PayoutResult:
    conversation_id: str
    settled: bool = False
    receipt_number: str = ""
    message: str = ""


def normalise_phone(phone):
    """07XXXXXXXX / +2547XXXXXXXX / 2547XXXXXXXX / 7XXXXXXXX -> 2547XXXXXXXX (Safaricom format)."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "17":
        digits = "254" + digits
    if not (len(digits) == 12 and digits.startswith("254") and digits[3] in "17"):
        raise ValueError("Enter a Kenyan mobile number, e.g. 0712 345 678.")
    return digits


class SimulatedProvider:
    name = "simulated"

    def stk_push(self, payment):
        code = "SIM" + uuid.uuid4().hex[:7].upper()
        return PushResult(checkout_request_id=f"ws_CO_SIM_{uuid.uuid4().hex[:12]}", settled=True,
                          receipt_number=code, message="Simulated payment completed")

    def stk_query(self, payment):
        return QueryResult("0", "Simulated payment completed")

    def b2c(self, payout):
        return PayoutResult(conversation_id=f"AG_SIM_{uuid.uuid4().hex[:12]}", settled=True,
                            receipt_number="SIM" + uuid.uuid4().hex[:7].upper())


class MpesaProvider:
    name = "mpesa"

    def __init__(self):
        self.host = DARAJA_HOSTS.get(settings.MPESA_ENVIRONMENT, DARAJA_HOSTS["sandbox"])
        missing = [k for k in ("MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET", "MPESA_SHORTCODE", "MPESA_PASSKEY",
                               "MPESA_CALLBACK_BASE_URL", "MPESA_CALLBACK_TOKEN") if not getattr(settings, k)]
        if missing:
            raise ProviderError(f"M-Pesa is not configured: set {', '.join(missing)} in .env")

    # -- HTTP --------------------------------------------------------------------------
    def _request(self, path, payload=None, auth=None):
        headers = {"Content-Type": "application/json"}
        headers["Authorization"] = auth or f"Bearer {self._token()}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.host + path, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed Safaricom host
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            logger.warning("Daraja %s failed: %s %s", path, exc.code, body[:500])
            try:
                detail = json.loads(body).get("errorMessage") or body
            except ValueError:
                detail = body
            raise ProviderError(f"M-Pesa error: {detail[:200]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError("Could not reach M-Pesa. Please try again.") from exc

    def _token(self):
        key = f"mpesa-token-{settings.MPESA_ENVIRONMENT}"
        token = cache.get(key)
        if token:
            return token
        basic = base64.b64encode(f"{settings.MPESA_CONSUMER_KEY}:{settings.MPESA_CONSUMER_SECRET}".encode()).decode()
        data = self._request("/oauth/v1/generate?grant_type=client_credentials", auth=f"Basic {basic}")
        token = data["access_token"]
        cache.set(key, token, int(data.get("expires_in", 3599)) - 60)
        return token

    def _password(self):
        stamp = timezone.localtime().strftime("%Y%m%d%H%M%S")
        raw = f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{stamp}"
        return base64.b64encode(raw.encode()).decode(), stamp

    def _callback(self, kind):
        base = settings.MPESA_CALLBACK_BASE_URL.rstrip("/")
        return f"{base}/payments/mpesa/{kind}/{settings.MPESA_CALLBACK_TOKEN}/"

    # -- Buyer payments (STK Push) ---------------------------------------------------
    def stk_push(self, payment):
        password, stamp = self._password()
        data = self._request("/mpesa/stkpush/v1/processrequest", {
            "BusinessShortCode": settings.MPESA_SHORTCODE, "Password": password, "Timestamp": stamp,
            "TransactionType": settings.MPESA_TRANSACTION_TYPE, "Amount": int(payment.amount),
            "PartyA": payment.phone, "PartyB": settings.MPESA_TILL_OR_PAYBILL or settings.MPESA_SHORTCODE,
            "PhoneNumber": payment.phone, "CallBackURL": self._callback("stk"),
            "AccountReference": payment.order.reference[:12], "TransactionDesc": f"Order {payment.order.reference}"[:13],
        })
        if str(data.get("ResponseCode")) != "0":
            raise ProviderError(data.get("ResponseDescription") or data.get("errorMessage") or "M-Pesa rejected the request.")
        return PushResult(checkout_request_id=data["CheckoutRequestID"], merchant_request_id=data.get("MerchantRequestID", ""),
                          message=data.get("CustomerMessage", ""))

    def stk_query(self, payment):
        password, stamp = self._password()
        try:
            data = self._request("/mpesa/stkpushquery/v1/query", {
                "BusinessShortCode": settings.MPESA_SHORTCODE, "Password": password, "Timestamp": stamp,
                "CheckoutRequestID": payment.checkout_request_id})
        except ProviderError as exc:
            if "being processed" in str(exc).lower():
                return QueryResult("", "Still waiting for the buyer")
            raise
        return QueryResult(str(data.get("ResultCode", "")), data.get("ResultDesc", ""))

    # -- Supplier payouts (B2C) --------------------------------------------------------
    def b2c(self, payout):
        missing = [k for k in ("MPESA_B2C_SHORTCODE", "MPESA_B2C_INITIATOR", "MPESA_B2C_SECURITY_CREDENTIAL")
                   if not getattr(settings, k)]
        if missing:
            raise ProviderError(f"M-Pesa payouts are not configured: set {', '.join(missing)} in .env")
        data = self._request("/mpesa/b2c/v3/paymentrequest", {
            "OriginatorConversationID": str(uuid.uuid4()),
            "InitiatorName": settings.MPESA_B2C_INITIATOR,
            "SecurityCredential": settings.MPESA_B2C_SECURITY_CREDENTIAL,
            "CommandID": "BusinessPayment", "Amount": int(payout.amount),
            "PartyA": settings.MPESA_B2C_SHORTCODE, "PartyB": payout.phone,
            "Remarks": f"Origin 254 {payout.order_line.order.reference}"[:100],
            "QueueTimeOutURL": self._callback("b2c-timeout"), "ResultURL": self._callback("b2c-result"),
            "Occasion": payout.reference[:100],
        })
        if str(data.get("ResponseCode")) != "0":
            raise ProviderError(data.get("ResponseDescription") or "M-Pesa rejected the payout.")
        return PayoutResult(conversation_id=data.get("ConversationID", ""), message=data.get("ResponseDescription", ""))


def get_provider(name=None):
    name = name or settings.PAYMENT_PROVIDER
    if name == "mpesa":
        return MpesaProvider()
    if name == "simulated":
        return SimulatedProvider()
    raise ProviderError(f"Unknown payment provider '{name}'.")
