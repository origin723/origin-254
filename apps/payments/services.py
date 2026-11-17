"""Payment workflow.

1. Order confirmed (every supplier reconfirmed) -> the buyer can pay the outstanding amount.
2. `start_payment` sends an M-Pesa prompt (STK Push) to the buyer's phone.
3. Safaricom calls back (`handle_stk_callback`) or the buyer taps "Check status"
   (`refresh_payment`). Success marks the money as received and held by Origin 254.
4. When the buyer confirms receipt of a supplier's lot, `release_payout` creates the
   supplier's payout (gross line total minus the platform fee). It is sent automatically
   with M-Pesa B2C when PAYOUT_PROVIDER is mpesa / simulated, or recorded by an admin.
"""
import math
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.comms.models import Notification
from apps.comms.notify import notify, notify_admins
from apps.core.audit import log_action
from apps.core.exceptions import WorkflowError

from .models import Payment, Payout
from .providers import ProviderError, get_provider, normalise_phone

PAYABLE_ORDER_STATUSES = {"confirmed", "in_progress", "delivered", "completed", "disputed"}
MPESA_MAX_AMOUNT = Decimal("250000")  # per-transaction M-Pesa limit


def amount_paid(order):
    return sum((p.amount for p in order.payments.filter(status=Payment.Status.SUCCEEDED)), Decimal(0))


def amount_due(order):
    """What the buyer still owes, rounded up to whole shillings (M-Pesa takes integers)."""
    due = Decimal(order.total) - amount_paid(order)
    return Decimal(math.ceil(due)) if due > 0 else Decimal(0)


def refund_due(order):
    over = amount_paid(order) - Decimal(order.total)
    return over if over >= 1 else Decimal(0)


def payment_state(order):
    paid, total = amount_paid(order), Decimal(order.total)
    if order.payments.filter(status=Payment.Status.PENDING).exists():
        return "pending"
    if total > 0 and paid + Decimal("0.99") >= total:
        return "paid"
    return "partial" if paid > 0 else "unpaid"


def is_paid(order):
    return payment_state(order) == "paid"


def commission_percent():
    return Decimal(str(settings.PLATFORM_FEE_PERCENT))


def commission_summary():
    """Origin 254's commission: earned (on paid payouts) and pending (on payouts not yet paid)."""
    from django.db.models import Sum
    earned = Payout.objects.filter(status=Payout.Status.PAID).aggregate(s=Sum("fee"))["s"] or Decimal(0)
    pending = Payout.objects.exclude(status=Payout.Status.PAID).aggregate(s=Sum("fee"))["s"] or Decimal(0)
    received = Payment.objects.filter(status=Payment.Status.SUCCEEDED).aggregate(s=Sum("amount"))["s"] or Decimal(0)
    paid_out = Payout.objects.filter(status=Payout.Status.PAID).aggregate(s=Sum("amount"))["s"] or Decimal(0)
    return {"percent": commission_percent(), "earned": earned, "pending": pending, "received": received,
            "paid_out": paid_out, "held": received - paid_out - earned}


def split(gross):
    """(commission, seller amount) for a sale value."""
    fee = _fee(Decimal(gross))
    return fee, Decimal(gross) - fee


def _fee(gross):
    pct = Decimal(str(settings.PLATFORM_FEE_PERCENT))
    return (gross * pct / 100).quantize(Decimal("0.01"))


# --- Buyer payments ---------------------------------------------------------------------

def start_payment(user, order, phone):
    if order.buyer_id != user.pk:
        raise WorkflowError("Only the buyer can pay for this order.", "permission_denied")
    if order.status not in PAYABLE_ORDER_STATUSES:
        raise WorkflowError("You can pay once every supplier has reconfirmed the order.", "not_payable")
    try:
        phone = normalise_phone(phone)
    except ValueError as exc:
        raise WorkflowError(str(exc), "invalid_phone") from exc
    with transaction.atomic():
        # Lock the order so two clicks cannot start two payments.
        type(order).objects.select_for_update().get(pk=order.pk)
        recent_pending = order.payments.filter(status=Payment.Status.PENDING,
                                               created_at__gte=timezone.now() - timedelta(minutes=2))
        if recent_pending.exists():
            raise WorkflowError("A payment request was just sent. Check your phone, or wait a minute and try again.",
                                "payment_pending")
        amount = amount_due(order)
        if amount <= 0:
            raise WorkflowError("This order is already paid.", "already_paid")
        if amount > MPESA_MAX_AMOUNT:
            amount = MPESA_MAX_AMOUNT  # pay in instalments; the rest stays due
        try:
            provider = get_provider()
        except ProviderError as exc:
            raise WorkflowError(str(exc), "payments_not_configured") from exc
        payment = Payment.objects.create(order=order, payer=user, amount=amount, phone=phone,
                                         provider=provider.name)
    try:
        result = provider.stk_push(payment)
    except ProviderError as exc:
        payment.status = Payment.Status.FAILED
        payment.result_description = str(exc)[:255]
        payment.save(update_fields=["status", "result_description", "updated_at"])
        raise WorkflowError(str(exc), "payment_failed") from exc
    payment.checkout_request_id = result.checkout_request_id
    payment.merchant_request_id = result.merchant_request_id
    payment.save(update_fields=["checkout_request_id", "merchant_request_id", "updated_at"])
    log_action(user, "payment.started", payment, amount=str(amount), order=order.reference)
    if result.settled:
        _settle(payment, "0", result.message or "Paid", receipt=result.receipt_number, amount=amount)
        payment.refresh_from_db()
    return payment


@transaction.atomic
def _settle(payment, result_code, description, receipt="", amount=None, phone="", raw=None):
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.PENDING:
        return payment  # callbacks can arrive twice; the first one wins
    payment.result_code = str(result_code)
    payment.result_description = (description or "")[:255]
    if raw is not None:
        payment.raw_callback = raw
    order = payment.order
    if str(result_code) == "0":
        if amount is not None and Decimal(str(amount)) != payment.amount:
            payment.status = Payment.Status.FAILED
            payment.result_description = f"Amount mismatch: expected {payment.amount}, received {amount}"
            notify_admins(f"Payment amount mismatch on {order.reference}", payment.result_description,
                          Notification.Kind.SYSTEM, f"/app/orders/{order.pk}/")
        else:
            payment.status = Payment.Status.SUCCEEDED
            payment.receipt_number = receipt or ""
            payment.paid_at = timezone.now()
    elif str(result_code) == "1032":
        payment.status = Payment.Status.CANCELLED
    else:
        payment.status = Payment.Status.FAILED
    payment.save()
    log_action(None, f"payment.{payment.status}", payment, receipt=payment.receipt_number,
               result=payment.result_description)
    from apps.orders.models import OrderEvent
    OrderEvent.objects.create(order=order, actor=payment.payer, event=f"payment_{payment.status}",
                              note=f"{payment.reference}: KES {payment.amount:,.0f}"
                                   + (f" · M-Pesa {payment.receipt_number}" if payment.receipt_number else "")
                                   + ("" if payment.status == Payment.Status.SUCCEEDED
                                      else f" · {payment.result_description}"))
    if payment.status == Payment.Status.SUCCEEDED:
        notify(payment.payer, f"Payment received for {order.reference}",
               f"KES {payment.amount:,.0f} received (M-Pesa {payment.receipt_number}). Origin 254 holds it until "
               "you confirm delivery.", Notification.Kind.ORDER, f"/app/orders/{order.pk}/")
        for line in order.lines.select_related("supplier__user"):
            notify(line.supplier.user, f"{order.reference} is paid",
                   "The buyer's payment is held by Origin 254 and is released to you when they confirm delivery.",
                   Notification.Kind.ORDER, f"/app/orders/{order.pk}/")
        # Lots the buyer already confirmed before paying can now be released.
        for line in order.lines.filter(status="completed"):
            release_payout(line)
    else:
        notify(payment.payer, f"Payment not completed for {order.reference}",
               payment.result_description or "The M-Pesa payment did not go through.", Notification.Kind.ORDER,
               f"/app/orders/{order.pk}/")
    return payment


def handle_stk_callback(body):
    """Safaricom's STK Push result. Returns the payment, or None if unknown."""
    cb = (body or {}).get("Body", {}).get("stkCallback", {})
    payment = Payment.objects.filter(checkout_request_id=cb.get("CheckoutRequestID", "") or "-").first()
    if not payment:
        return None
    meta = {i.get("Name"): i.get("Value") for i in cb.get("CallbackMetadata", {}).get("Item", [])}
    return _settle(payment, cb.get("ResultCode"), cb.get("ResultDesc", ""), receipt=meta.get("MpesaReceiptNumber", ""),
                   amount=meta.get("Amount") if str(cb.get("ResultCode")) == "0" else None, raw=body)


def refresh_payment(user, payment):
    """Ask M-Pesa for the result when the callback has not arrived (e.g. no public URL in development)."""
    if not (payment.payer_id == user.pk or user.is_platform_admin):
        raise WorkflowError("Not allowed.", "permission_denied")
    if payment.status != Payment.Status.PENDING:
        return payment
    try:
        result = get_provider(payment.provider).stk_query(payment)
    except ProviderError as exc:
        raise WorkflowError(str(exc), "payment_query_failed") from exc
    if result.result_code == "":
        return payment
    if result.result_code == "0":
        # The query does not return the receipt; keep waiting for the callback unless simulated.
        if payment.provider == "simulated":
            return _settle(payment, "0", result.description, receipt="SIMQUERY")
        return payment
    return _settle(payment, result.result_code, result.description)


# --- Supplier payouts ----------------------------------------------------------------------

@transaction.atomic
def release_payout(line):
    """Called when the buyer confirms receipt of a lot. Creates (and, if automatic, sends) the payout."""
    order = line.order
    if hasattr(line, "payout") or not is_paid(order):
        return getattr(line, "payout", None)
    gross = line.total
    fee = _fee(gross)
    on_hold = order.disputes.filter(status__in=["open", "under_review"]).exists()
    payout = Payout.objects.create(order_line=line, supplier=line.supplier, gross_amount=gross, fee=fee,
                                   amount=gross - fee, phone=line.supplier.user.phone,
                                   status=Payout.Status.ON_HOLD if on_hold else Payout.Status.READY)
    log_action(None, "payout.created", payout, amount=str(payout.amount), status=payout.status)
    if payout.status == Payout.Status.READY:
        if settings.PAYOUT_PROVIDER in {"mpesa", "simulated"}:
            transaction.on_commit(lambda: _auto_send(payout.pk))
        else:
            notify_admins(f"Payout ready: KES {payout.amount:,.0f} to {line.supplier}",
                          f"{order.reference}. Pay and record it under Payments.", Notification.Kind.ORDER,
                          "/app/admin/payments/")
    return payout


def _auto_send(payout_id):
    payout = Payout.objects.get(pk=payout_id)
    try:
        send_payout(None, payout)
    except WorkflowError as exc:
        notify_admins(f"Automatic payout failed: {payout.reference}", exc.message, Notification.Kind.SYSTEM,
                      "/app/admin/payments/")


def send_payout(admin, payout):
    if admin is not None and not admin.is_platform_admin:
        raise WorkflowError("Only administrators send payouts.", "permission_denied")
    if payout.status not in {Payout.Status.READY, Payout.Status.FAILED}:
        raise WorkflowError(f"This payout is {payout.get_status_display().lower()}.", "payout_not_ready")
    try:
        payout.phone = normalise_phone(payout.phone or payout.supplier.user.phone)
    except ValueError as exc:
        raise WorkflowError(f"The supplier has no valid M-Pesa number: {exc}", "invalid_phone") from exc
    provider_name = "simulated" if settings.PAYOUT_PROVIDER == "simulated" else "mpesa"
    try:
        result = get_provider(provider_name).b2c(payout)
    except ProviderError as exc:
        payout.status, payout.result_description = Payout.Status.FAILED, str(exc)[:255]
        payout.save(update_fields=["status", "result_description", "phone", "updated_at"])
        raise WorkflowError(str(exc), "payout_failed") from exc
    payout.method = Payout.Method.SIMULATED if provider_name == "simulated" else Payout.Method.MPESA_B2C
    payout.conversation_id = result.conversation_id
    payout.status = Payout.Status.PROCESSING
    payout.paid_by = admin
    payout.save()
    log_action(admin, "payout.sent", payout, conversation=result.conversation_id)
    if result.settled:
        return _payout_done(payout, True, result.receipt_number, "Paid")
    return payout


@transaction.atomic
def _payout_done(payout, ok, receipt="", description="", raw=None):
    payout = Payout.objects.select_for_update().get(pk=payout.pk)
    if payout.status != Payout.Status.PROCESSING:
        return payout
    payout.status = Payout.Status.PAID if ok else Payout.Status.FAILED
    payout.receipt_number = receipt or ""
    payout.result_description = (description or "")[:255]
    if raw is not None:
        payout.raw_result = raw
    if ok:
        payout.paid_at = timezone.now()
    payout.save()
    _after_payout(payout)
    return payout


def _after_payout(payout):
    order = payout.order_line.order
    log_action(payout.paid_by, f"payout.{payout.status}", payout, receipt=payout.receipt_number)
    if payout.status == Payout.Status.PAID:
        from apps.orders.models import OrderEvent
        OrderEvent.objects.create(order=order, actor=payout.paid_by, event="payout_paid", order_line=payout.order_line,
                                  note=f"KES {payout.amount:,.0f} paid to {payout.supplier}"
                                       + (f" · {payout.receipt_number}" if payout.receipt_number else ""))
        notify(payout.supplier.user, f"You've been paid KES {payout.amount:,.0f} for {order.reference}",
               (f"M-Pesa {payout.receipt_number}. " if payout.receipt_number else "")
               + (f"Platform fee KES {payout.fee:,.2f}." if payout.fee else ""), Notification.Kind.ORDER,
               f"/app/orders/{order.pk}/")
    else:
        notify_admins(f"Payout failed: {payout.reference}", payout.result_description, Notification.Kind.SYSTEM,
                      "/app/admin/payments/")


def handle_b2c_result(body):
    res = (body or {}).get("Result", {})
    payout = Payout.objects.filter(conversation_id=res.get("ConversationID", "") or "-").first()
    if not payout:
        return None
    ok = str(res.get("ResultCode")) == "0"
    return _payout_done(payout, ok, res.get("TransactionID", "") if ok else "", res.get("ResultDesc", ""), raw=body)


def handle_b2c_timeout(body):
    res = (body or {}).get("Result", body or {})
    payout = Payout.objects.filter(conversation_id=res.get("ConversationID", "") or "-").first()
    if payout:
        return _payout_done(payout, False, description="M-Pesa timed out; check the B2C account and retry.", raw=body)
    return None


@transaction.atomic
def record_manual_payout(admin, payout, receipt_number, note=""):
    if not admin.is_platform_admin:
        raise WorkflowError("Only administrators record payouts.", "permission_denied")
    if payout.status not in {Payout.Status.READY, Payout.Status.FAILED}:
        raise WorkflowError(f"This payout is {payout.get_status_display().lower()}.", "payout_not_ready")
    receipt_number = (receipt_number or "").strip().upper()
    if len(receipt_number) < 6:
        raise WorkflowError("Enter the M-Pesa or bank transaction code.", "receipt_required")
    payout.status, payout.method = Payout.Status.PAID, Payout.Method.MANUAL
    payout.receipt_number, payout.result_description = receipt_number[:40], note[:255]
    payout.paid_by, payout.paid_at = admin, timezone.now()
    payout.save()
    _after_payout(payout)
    return payout


@transaction.atomic
def release_hold(admin, payout):
    if not admin.is_platform_admin:
        raise WorkflowError("Only administrators release held payouts.", "permission_denied")
    if payout.status != Payout.Status.ON_HOLD:
        raise WorkflowError("This payout is not on hold.")
    payout.status = Payout.Status.READY
    payout.save(update_fields=["status", "updated_at"])
    log_action(admin, "payout.released_from_hold", payout)
    if settings.PAYOUT_PROVIDER in {"mpesa", "simulated"}:
        transaction.on_commit(lambda: _auto_send(payout.pk))
    return payout


@transaction.atomic
def mark_refunded(admin, order, receipt_number, amount):
    """Record a refund the admin made outside the system (e.g. M-Pesa reversal) for an overpaid order."""
    if not admin.is_platform_admin:
        raise WorkflowError("Only administrators record refunds.", "permission_denied")
    amount = Decimal(str(amount))
    if amount <= 0 or amount > refund_due(order):
        raise WorkflowError(f"The refund must be between KES 1 and KES {refund_due(order):,.2f}.", "invalid_amount")
    refund = Payment.objects.create(order=order, payer=order.buyer, amount=-amount, phone="", provider="manual",
                                    status=Payment.Status.SUCCEEDED, receipt_number=(receipt_number or "").strip().upper()[:40],
                                    result_description="Refund to buyer", paid_at=timezone.now())
    log_action(admin, "payment.refunded", refund, amount=str(amount))
    notify(order.buyer, f"Refund of KES {amount:,.0f} for {order.reference}", f"Reference {refund.receipt_number}.",
           Notification.Kind.ORDER, f"/app/orders/{order.pk}/")
    return refund
