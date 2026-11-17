"""Procurement workflow after quotations are accepted.

Order created (lines AWAITING_RECONFIRMATION)
  -> supplier reconfirms availability (inventory reserved) -> CONFIRMED
  -> pre-dispatch quality inspection -> INSPECTION_PASSED / INSPECTION_FAILED
  -> delivery: scheduled -> picked up (IN_TRANSIT) -> delivered (DELIVERED)
  -> buyer confirms receipt -> COMPLETED
  -> feedback (apps.reviews)
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.catalog.models import InventoryItem, from_base_quantity, to_base_quantity
from apps.comms.models import Notification
from apps.comms.notify import notify, notify_admins
from apps.core.audit import log_action
from apps.core.exceptions import WorkflowError
from apps.sourcing.models import DeliveryArrangement, Match, Quotation, SourcingRequest

from .models import Delivery, Dispute, Inspection, Order, OrderEvent, OrderLine

S = OrderLine.Status


def _event(order, actor, event, note="", line=None):
    OrderEvent.objects.create(order=order, order_line=line, actor=actor, event=event, note=note)


def can_view_order(user, order):
    if user.is_platform_admin or order.buyer_id == user.pk:
        return True
    if user.is_supplier and order.lines.filter(supplier__user=user).exists():
        return True
    if user.is_logistics and order.lines.filter(delivery__logistics_partner=user).exists():
        return True
    return False


def refresh_order_status(order):
    lines = list(order.lines.all())
    active = [line for line in lines if line.status not in OrderLine.INACTIVE_STATUSES]
    statuses = {line.status for line in active}
    if order.disputes.filter(status__in=[Dispute.Status.OPEN, Dispute.Status.UNDER_REVIEW]).exists():
        new = Order.Status.DISPUTED
    elif not active:
        new = Order.Status.CANCELLED
    elif S.AWAITING_RECONFIRMATION in statuses:
        new = Order.Status.PENDING_CONFIRMATION
    elif statuses == {S.COMPLETED}:
        new = Order.Status.COMPLETED
    elif statuses <= {S.DELIVERED, S.COMPLETED}:
        new = Order.Status.DELIVERED
    elif statuses & {S.INSPECTION_PASSED, S.IN_TRANSIT, S.DELIVERED, S.COMPLETED}:
        new = Order.Status.IN_PROGRESS
    else:
        new = Order.Status.CONFIRMED

    if new != order.status:
        order.status = new
        if new == Order.Status.COMPLETED:
            order.completed_at = timezone.now()
        order.save(update_fields=["status", "completed_at", "updated_at"])

    request = order.sourcing_request
    request_status = {
        Order.Status.CONFIRMED: SourcingRequest.Status.SUPPLY_CONFIRMED,
        Order.Status.IN_PROGRESS: SourcingRequest.Status.IN_FULFILMENT,
        Order.Status.DELIVERED: SourcingRequest.Status.IN_FULFILMENT,
        Order.Status.COMPLETED: SourcingRequest.Status.COMPLETED,
    }.get(new)
    if request_status and request.status != request_status and request.status != SourcingRequest.Status.CANCELLED:
        if request_status == SourcingRequest.Status.COMPLETED and request.orders.exclude(
                status__in=[Order.Status.COMPLETED, Order.Status.CANCELLED]).exists():
            return order
        request.status = request_status
        request.save(update_fields=["status", "updated_at"])
    return order


# --- Order creation and supplier reconfirmation ------------------------------------

@transaction.atomic
def create_order(buyer, request, quotations, notes=""):
    if request.buyer_id != buyer.pk:
        raise WorkflowError("Only the buyer who created this request can place an order.", "permission_denied")
    if not request.is_active:
        raise WorkflowError(f"Request {request.reference} is {request.get_status_display().lower()}.")
    quotations = list(quotations)
    if not quotations:
        raise ValidationError({"quotations": "Select at least one accepted quotation."})
    for q in quotations:
        if q.sourcing_request_id != request.pk:
            raise ValidationError({"quotations": f"{q.reference} belongs to another request."})
        if q.status != Quotation.Status.ACCEPTED:
            raise ValidationError({"quotations": f"{q.reference} has not been accepted."})
        if hasattr(q, "order_line"):
            raise ValidationError({"quotations": f"{q.reference} is already on order {q.order_line.order}."})

    order = Order.objects.create(sourcing_request=request, buyer=buyer,
                                 delivery_location=request.delivery_location, notes=notes)
    for q in quotations:
        line = OrderLine.objects.create(
            order=order, supplier=q.supplier, quotation=q, inventory_item=q.inventory_item,
            quantity=q.quantity, unit=request.unit, price_per_unit=q.price_per_unit,
            delivery_cost=q.delivery_cost, quality_grade=request.min_quality_grade,
            quality_specifications={"grade": request.min_quality_grade,
                                    "specifications": request.quality_specifications,
                                    "notes": request.quality_notes,
                                    "packaging": request.packaging_requirements,
                                    "quoted_grade": q.quality_grade},
            agreed_delivery_date=max(q.earliest_delivery_date, timezone.localdate()),
        )
        notify(q.supplier.user, f"Order {order.reference}: please reconfirm availability",
               f"{line.quantity:g} {line.unit} {request.product.name} for delivery by "
               f"{line.agreed_delivery_date:%d %b}. Reconfirm so the order can be confirmed.",
               Notification.Kind.ORDER, f"/app/orders/{order.pk}/")
    _event(order, buyer, "order_created", f"{len(quotations)} supplier(s)")
    log_action(buyer, "order.created", order, lines=len(quotations), total=str(order.total))
    return order


@transaction.atomic
def reconfirm_line(user, line, available=True, note=""):
    """Supplier reconfirms stock right before the order is confirmed (business rule).

    Reserves the quantity by decrementing inventory under a row lock so two
    orders cannot both claim the same produce.
    """
    if not user.is_supplier or line.supplier.user_id != user.pk:
        raise WorkflowError("Only the supplier on this line can reconfirm it.", "permission_denied")
    if line.status != S.AWAITING_RECONFIRMATION:
        raise WorkflowError("This line has already been reconfirmed or closed.")

    order = line.order
    item = InventoryItem.objects.select_for_update().get(pk=line.inventory_item_id)
    if available:
        needed = Decimal(str(from_base_quantity(to_base_quantity(line.quantity, line.unit), item.unit)))
        if not item.is_active or item.quantity_available < needed:
            raise WorkflowError(
                f"Only {item.quantity_available:g} {item.unit} in stock; update your inventory or decline.",
                "insufficient_stock")
        item.quantity_available -= needed
        item.last_confirmed_at = timezone.now()
        item.save(update_fields=["quantity_available", "last_confirmed_at", "updated_at"])
        line.status = S.CONFIRMED
        line.reconfirmed_at = timezone.now()
        line.save(update_fields=["status", "reconfirmed_at", "updated_at"])
        Match.objects.filter(sourcing_request=order.sourcing_request, inventory_item=item).update(
            status=Match.Status.COMMITTED, confirmed_quantity=line.quantity)
        arrangement = order.sourcing_request.delivery_arrangement
        pickup, dropoff = item.location, order.delivery_location
        Delivery.objects.create(order_line=line, pickup_location=pickup, dropoff_location=dropoff,
                                scheduled_date=line.agreed_delivery_date if
                                arrangement != DeliveryArrangement.PLATFORM_LOGISTICS else None)
        _event(order, user, "supply_confirmed", note, line)
        title = f"{line.supplier} confirmed supply on {order.reference}"
    else:
        line.status = S.CANCELLED
        line.save(update_fields=["status", "updated_at"])
        Quotation.objects.filter(pk=line.quotation_id).update(status=Quotation.Status.WITHDRAWN)
        _event(order, user, "supplier_declined", note, line)
        title = f"{line.supplier} can no longer supply {order.reference}"
    log_action(user, "order_line.reconfirmed" if available else "order_line.declined", line, note=note)
    notify(order.buyer, title, note, Notification.Kind.ORDER, f"/app/orders/{order.pk}/")
    refresh_order_status(order)
    return line


@transaction.atomic
def cancel_order(user, order, reason=""):
    if not (user.is_platform_admin or order.buyer_id == user.pk):
        raise WorkflowError("Not allowed.", "permission_denied")
    if order.lines.filter(status__in=[S.IN_TRANSIT, S.DELIVERED, S.COMPLETED]).exists():
        raise WorkflowError("Goods are already in transit or delivered; raise a dispute instead.")
    for line in order.lines.exclude(status__in=OrderLine.INACTIVE_STATUSES):
        if line.status != S.AWAITING_RECONFIRMATION:
            # Release reserved stock.
            item = InventoryItem.objects.select_for_update().get(pk=line.inventory_item_id)
            item.quantity_available += Decimal(str(from_base_quantity(
                to_base_quantity(line.quantity, line.unit), item.unit)))
            item.save(update_fields=["quantity_available", "updated_at"])
        line.status = S.CANCELLED
        line.save(update_fields=["status", "updated_at"])
        notify(line.supplier.user, f"Order {order.reference} was cancelled", reason, Notification.Kind.ORDER)
    order.status = Order.Status.CANCELLED
    order.save(update_fields=["status", "updated_at"])
    _event(order, user, "order_cancelled", reason)
    log_action(user, "order.cancelled", order, reason=reason)
    return order


# --- Quality inspection -----------------------------------------------------------

@transaction.atomic
def record_inspection(user, line, stage, result, grade_observed="", measurements=None,
                      accepted_quantity=None, rejected_quantity=None, notes="", report=None):
    order = line.order
    if not (user.is_platform_admin or order.buyer_id == user.pk):
        raise WorkflowError("Inspections are recorded by Origin 254 QA officers or the buyer.", "permission_denied")
    if stage == Inspection.Stage.PRE_DISPATCH and line.status not in {S.CONFIRMED, S.INSPECTION_FAILED}:
        raise WorkflowError("Pre-dispatch inspection is done after supply is confirmed and before pickup.")
    if stage == Inspection.Stage.ON_RECEIPT and line.status != S.DELIVERED:
        raise WorkflowError("On-receipt inspection is done after delivery.")
    if result not in Inspection.Result.values:
        raise ValidationError({"result": "Invalid inspection result."})
    if accepted_quantity is not None and Decimal(str(accepted_quantity)) > line.quantity:
        raise ValidationError({"accepted_quantity": "Cannot exceed the ordered quantity."})

    inspection = Inspection.objects.create(
        order_line=line, stage=stage, inspector=user, result=result, grade_observed=grade_observed,
        measurements=measurements or {}, accepted_quantity=accepted_quantity,
        rejected_quantity=rejected_quantity, notes=notes, report=report,
    )
    if stage == Inspection.Stage.PRE_DISPATCH:
        passed = result in {Inspection.Result.PASSED, Inspection.Result.CONDITIONAL}
        line.status = S.INSPECTION_PASSED if passed else S.INSPECTION_FAILED
        if passed and accepted_quantity is not None and Decimal(str(accepted_quantity)) < line.quantity:
            line.quantity = Decimal(str(accepted_quantity))
        line.save(update_fields=["status", "quantity", "updated_at"])
    _event(order, user, f"inspection_{stage}_{result}", notes, line)
    log_action(user, "inspection.recorded", inspection, result=result, stage=stage)
    for recipient in {order.buyer, line.supplier.user} - {user}:
        notify(recipient, f"Inspection {result} for {line.supplier} on {order.reference}", notes,
               Notification.Kind.INSPECTION, f"/app/orders/{order.pk}/")
    refresh_order_status(order)
    return inspection


# --- Delivery ---------------------------------------------------------------------

DELIVERY_FLOW = {
    Delivery.Status.PENDING: {Delivery.Status.SCHEDULED, Delivery.Status.FAILED},
    Delivery.Status.SCHEDULED: {Delivery.Status.PICKED_UP, Delivery.Status.FAILED},
    Delivery.Status.PICKED_UP: {Delivery.Status.IN_TRANSIT, Delivery.Status.DELIVERED, Delivery.Status.FAILED},
    Delivery.Status.IN_TRANSIT: {Delivery.Status.DELIVERED, Delivery.Status.FAILED},
    Delivery.Status.FAILED: {Delivery.Status.SCHEDULED},
    Delivery.Status.DELIVERED: set(),
}


def can_update_delivery(user, delivery):
    line = delivery.order_line
    if user.is_platform_admin:
        return True
    if delivery.logistics_partner_id:
        return delivery.logistics_partner_id == user.pk
    arrangement = line.order.sourcing_request.delivery_arrangement
    if arrangement == DeliveryArrangement.BUYER_COLLECTS:
        return line.order.buyer_id == user.pk
    return line.supplier.user_id == user.pk


@transaction.atomic
def assign_logistics(user, delivery, partner, scheduled_date=None):
    order = delivery.order_line.order
    if not (user.is_platform_admin or order.buyer_id == user.pk):
        raise WorkflowError("Only the buyer or an administrator can assign a logistics partner.",
                            "permission_denied")
    if not partner.is_logistics or not partner.is_verified:
        raise ValidationError({"logistics_partner": "Choose a verified logistics partner."})
    if delivery.status not in {Delivery.Status.PENDING, Delivery.Status.SCHEDULED, Delivery.Status.FAILED}:
        raise WorkflowError("This delivery is already under way.")
    delivery.logistics_partner = partner
    delivery.scheduled_date = scheduled_date or delivery.scheduled_date or delivery.order_line.agreed_delivery_date
    delivery.status = Delivery.Status.SCHEDULED
    delivery.log(user, "scheduled", f"Assigned to {partner.display_name}")
    delivery.save()
    _event(order, user, "logistics_assigned", partner.display_name, delivery.order_line)
    notify(partner, f"Delivery job for {order.reference}",
           f"Pick up {delivery.order_line.quantity:g} {delivery.order_line.unit} at {delivery.pickup_location}, "
           f"deliver to {delivery.dropoff_location} on {delivery.scheduled_date:%d %b}.",
           Notification.Kind.DELIVERY, "/app/deliveries/")
    return delivery


@transaction.atomic
def update_delivery(user, delivery, status, note="", received_quantity=None, received_by="",
                    vehicle_reg="", driver_name="", driver_phone="", proof_of_delivery=None):
    if not can_update_delivery(user, delivery):
        raise WorkflowError("You are not responsible for this delivery.", "permission_denied")
    if status not in DELIVERY_FLOW.get(delivery.status, set()):
        raise WorkflowError(f"Cannot move a delivery from {delivery.status} to {status}.")
    line, order = delivery.order_line, delivery.order_line.order

    if status == Delivery.Status.PICKED_UP and line.status != S.INSPECTION_PASSED:
        raise WorkflowError("Produce must pass pre-dispatch quality inspection before pickup.",
                            "inspection_required")
    if status == Delivery.Status.PICKED_UP:
        from django.conf import settings
        from apps.payments.services import is_paid
        if settings.REQUIRE_PAYMENT_BEFORE_DISPATCH and not is_paid(order):
            raise WorkflowError("The buyer has not paid for this order yet. Wait for payment before dispatch.",
                                "payment_required")
    if status == Delivery.Status.DELIVERED:
        if received_quantity is None or not received_by:
            raise ValidationError({"received_quantity": "Record the quantity received and who received it."})
        if Decimal(str(received_quantity)) > line.quantity:
            raise ValidationError({"received_quantity": "Cannot exceed the dispatched quantity."})

    now = timezone.now()
    delivery.status = status
    for attr, value in {"vehicle_reg": vehicle_reg, "driver_name": driver_name, "driver_phone": driver_phone}.items():
        if value:
            setattr(delivery, attr, value)
    if status == Delivery.Status.SCHEDULED and not delivery.scheduled_date:
        delivery.scheduled_date = line.agreed_delivery_date
    if status == Delivery.Status.PICKED_UP:
        delivery.picked_up_at = now
        line.status = S.IN_TRANSIT
    if status == Delivery.Status.DELIVERED:
        delivery.delivered_at = now
        delivery.received_quantity = received_quantity
        delivery.received_by = received_by
        if proof_of_delivery:
            delivery.proof_of_delivery = proof_of_delivery
        line.status = S.DELIVERED
        line.delivered_quantity = received_quantity
    delivery.log(user, status, note)
    delivery.save()
    line.save(update_fields=["status", "delivered_quantity", "updated_at"])
    _event(order, user, f"delivery_{status}", note, line)
    for recipient in {order.buyer, line.supplier.user} - {user}:
        notify(recipient, f"Delivery {status.replace('_', ' ')}: {line.supplier} → {order.reference}", note,
               Notification.Kind.DELIVERY, f"/app/orders/{order.pk}/")
    refresh_order_status(order)
    return delivery


# --- Completion and disputes ----------------------------------------------------------

def record_sale(line):
    """Save a completed order line to the buyer's purchase and the supplier's sales history (idempotent)."""
    from .models import SaleRecord
    item, order, loc = line.inventory_item, line.order, line.order.delivery_location
    quantity = line.delivered_quantity if line.delivered_quantity is not None else line.quantity
    record, _ = SaleRecord.objects.get_or_create(order_line=line, defaults={
        "order": order, "order_reference": order.reference,
        "buyer": order.buyer, "buyer_name": order.buyer.display_name,
        "supplier": line.supplier, "supplier_name": line.supplier.business_name,
        "inventory_item": item, "product_name": item.product.name,
        "variety_name": item.variety.name if item.variety_id else "",
        "quality_grade": line.quality_grade, "quantity": quantity, "unit": line.unit,
        "price_per_unit": line.price_per_unit, "delivery_cost": line.delivery_cost,
        "total": (quantity * line.price_per_unit + line.delivery_cost).quantize(Decimal("0.01")),
        "delivered_to": f"{loc.name}, {loc.county}",
        "completed_at": line.completed_at or timezone.now()})
    return record


@transaction.atomic
def complete_line(user, line, note=""):
    """Buyer confirms receipt and closes the supply agreement."""
    order = line.order
    if not (order.buyer_id == user.pk or user.is_platform_admin):
        raise WorkflowError("Only the buyer can confirm completion.", "permission_denied")
    if line.status != S.DELIVERED:
        raise WorkflowError("Only delivered lines can be completed.")
    line.status = S.COMPLETED
    line.completed_at = timezone.now()
    line.save(update_fields=["status", "completed_at", "updated_at"])
    _event(order, user, "line_completed", note, line)
    record_sale(line)
    from apps.payments.services import release_payout
    release_payout(line)
    log_action(user, "order_line.completed", line, delivered=str(line.delivered_quantity))
    notify(line.supplier.user, f"{order.reference} completed — please rate the buyer",
           "Thank you! The transaction is recorded as complete.", Notification.Kind.ORDER,
           f"/app/orders/{order.pk}/")
    from apps.reviews.services import recompute_supplier_metrics
    recompute_supplier_metrics(line.supplier)
    refresh_order_status(order)
    return line


@transaction.atomic
def raise_dispute(user, order, reason, line=None):
    if not can_view_order(user, order):
        raise WorkflowError("Not allowed.", "permission_denied")
    if not reason.strip():
        raise ValidationError({"reason": "Describe the problem."})
    dispute = Dispute.objects.create(order=order, order_line=line, raised_by=user, reason=reason)
    _event(order, user, "dispute_raised", reason, line)
    log_action(user, "dispute.raised", dispute)
    notify_admins(f"Dispute raised on {order.reference}", reason, Notification.Kind.DISPUTE,
                  f"/app/admin/disputes/{dispute.pk}/")
    refresh_order_status(order)
    return dispute


@transaction.atomic
def resolve_dispute(admin, dispute, resolution, status=Dispute.Status.RESOLVED):
    if not admin.is_platform_admin:
        raise WorkflowError("Only administrators resolve disputes.", "permission_denied")
    if status not in {Dispute.Status.UNDER_REVIEW, Dispute.Status.RESOLVED, Dispute.Status.REJECTED}:
        raise ValidationError({"status": "Invalid dispute status."})
    dispute.status = status
    dispute.resolution = resolution
    if status != Dispute.Status.UNDER_REVIEW:
        dispute.resolved_by = admin
        dispute.resolved_at = timezone.now()
    dispute.save()
    _event(dispute.order, admin, f"dispute_{status}", resolution, dispute.order_line)
    log_action(admin, f"dispute.{status}", dispute, resolution=resolution)
    notify(dispute.raised_by, f"Dispute on {dispute.order.reference}: {dispute.get_status_display()}",
           resolution, Notification.Kind.DISPUTE, f"/app/orders/{dispute.order_id}/")
    refresh_order_status(dispute.order)
    return dispute
