"""Business operations for sourcing requests, matches and quotations.

Views (API and web) call these functions; they enforce permissions, state
transitions and business rules in one place.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import VerificationStatus
from apps.catalog.models import GRADE_RANK, from_base_quantity, units_compatible
from apps.comms.models import Notification
from apps.comms.notify import notify
from apps.core.audit import log_action
from apps.core.exceptions import WorkflowError

from .matching import run_matching
from .models import Match, Quotation, SourcingRequest


def _require_buyer_owner(user, request):
    if request.buyer_id != user.pk:
        raise WorkflowError("Only the buyer who created this request can do this.", "permission_denied")


def _require_active(request):
    if not request.is_active:
        raise WorkflowError(f"Request {request.reference} is {request.get_status_display().lower()}.")


def _supplier_profile(user):
    profile = getattr(user, "supplier_profile", None) if user.is_supplier else None
    if profile is None:
        raise WorkflowError("A supplier profile is required.", "permission_denied")
    if profile.verification_status != VerificationStatus.APPROVED:
        raise WorkflowError("Your supplier account must be approved before you can do this.", "not_verified")
    return profile


# --- Sourcing requests --------------------------------------------------------

@transaction.atomic
def create_sourcing_request(buyer, **data):
    if not buyer.is_buyer:
        raise WorkflowError("Only food entrepreneurs and processors can create sourcing requests.",
                            "permission_denied")
    if not buyer.is_verified:
        raise WorkflowError("Your buyer account must be verified before you can submit sourcing requests.",
                            "not_verified")
    request = SourcingRequest(buyer=buyer, **data)
    request.full_clean(exclude=["reference"])
    request.save()
    log_action(buyer, "sourcing_request.created", request, quantity=str(request.quantity),
               product=request.product.name)
    result = run_matching(request)
    return request, result


@transaction.atomic
def cancel_sourcing_request(buyer, request, reason=""):
    _require_buyer_owner(buyer, request)
    if request.orders.exclude(status__in=["cancelled"]).exists():
        raise WorkflowError("This request has active orders; cancel those first.")
    _require_active(request)
    request.status = SourcingRequest.Status.CANCELLED
    request.save(update_fields=["status", "updated_at"])
    request.quotations.filter(status__in=Quotation.OPEN_STATUSES).update(status=Quotation.Status.EXPIRED)
    for match in request.matches.filter(status__in=[Match.Status.CONTACTED, Match.Status.AVAILABILITY_CONFIRMED]):
        notify(match.supplier.user, f"{request.reference} was cancelled by the buyer", reason,
               Notification.Kind.MATCH)
    log_action(buyer, "sourcing_request.cancelled", request, reason=reason)
    return request


def rerun_matching(user, request):
    if not (user.is_platform_admin or request.buyer_id == user.pk):
        raise WorkflowError("Not allowed.", "permission_denied")
    _require_active(request)
    return run_matching(request)


@transaction.atomic
def notify_matched_suppliers(buyer, request, match_ids=None, message=""):
    """Ask matched suppliers to confirm availability (supplier notification step)."""
    _require_buyer_owner(buyer, request)
    _require_active(request)
    matches = request.matches.filter(status=Match.Status.POTENTIAL).select_related("supplier__user")
    if match_ids:
        matches = matches.filter(pk__in=match_ids)
    now, notified = timezone.now(), []
    for match in matches:
        match.status = Match.Status.CONTACTED
        match.notified_at = now
        match.save(update_fields=["status", "notified_at", "updated_at"])
        notify(match.supplier.user,
               f"New sourcing opportunity: {request.quantity:g} {request.unit} {request.product.name}",
               f"{request.reference} for {request.intended_application}, deliver to "
               f"{request.delivery_location} by {request.delivery_deadline:%d %b %Y}. "
               f"Please confirm your availability. {message}".strip(),
               Notification.Kind.MATCH, f"/app/opportunities/{match.pk}/")
        notified.append(match)
    return notified


# --- Supplier availability ----------------------------------------------------

@transaction.atomic
def respond_to_match(user, match, available, quantity=None, note=""):
    profile = _supplier_profile(user)
    if match.supplier_id != profile.pk:
        raise WorkflowError("This opportunity belongs to another supplier.", "permission_denied")
    _require_active(match.sourcing_request)
    if match.status not in {Match.Status.POTENTIAL, Match.Status.CONTACTED, Match.Status.AVAILABILITY_CONFIRMED}:
        raise WorkflowError(f"This match is {match.get_status_display().lower()}.")

    request = match.sourcing_request
    match.responded_at = timezone.now()
    match.supplier_note = note
    if available:
        qty = Decimal(str(quantity)) if quantity is not None else match.available_quantity
        stock = Decimal(str(from_base_quantity(match.inventory_item.base_quantity, request.unit)))
        if qty <= 0 or qty > stock:
            raise WorkflowError(f"Confirmed quantity must be between 0 and your listed stock ({stock:g}).",
                                "invalid_quantity")
        match.status = Match.Status.AVAILABILITY_CONFIRMED
        match.confirmed_quantity = qty
        # Confirming availability also confirms the inventory listing is current.
        match.inventory_item.last_confirmed_at = timezone.now()
        match.inventory_item.save(update_fields=["last_confirmed_at"])
        title = f"{profile.business_name} confirmed {qty:g} {request.unit} available"
    else:
        match.status = Match.Status.DECLINED
        title = f"{profile.business_name} declined {request.reference}"
    match.save()
    notify(request.buyer, title, note, Notification.Kind.AVAILABILITY, f"/app/requests/{request.pk}/")
    return match


# --- Quotations -----------------------------------------------------------------

@transaction.atomic
def submit_quotation(user, request, inventory_item, quantity, price_per_unit, earliest_delivery_date,
                     valid_until, delivery_cost=0, includes_delivery=False, notes=""):
    profile = _supplier_profile(user)
    _require_active(request)
    if inventory_item.supplier_id != profile.pk:
        raise WorkflowError("You can only quote from your own inventory.", "permission_denied")
    if not request.matches.filter(supplier=profile).exclude(status=Match.Status.EXPIRED).exists():
        raise WorkflowError("You can only quote on requests you have been matched to.", "not_matched")
    if inventory_item.product_id != request.product_id:
        raise WorkflowError("The inventory product does not match the request.", "product_mismatch")
    if not inventory_item.has_required_photos:
        from django.conf import settings
        raise WorkflowError(f"Add at least {settings.MIN_PHOTOS_PER_LISTING} photos of this produce before quoting.",
                            "photos_required")
    if not units_compatible(inventory_item.unit, request.unit):
        raise WorkflowError(f"This listing is in {inventory_item.get_unit_display().lower()} but the buyer asked for "
                            f"{request.get_unit_display().lower()}.", "unit_mismatch")
    if GRADE_RANK[inventory_item.quality_grade] < request.required_grade_rank - 1:
        raise WorkflowError("This produce is below the requested quality grade.", "quality_mismatch")

    quantity = Decimal(str(quantity))
    stock = Decimal(str(from_base_quantity(inventory_item.base_quantity, request.unit)))
    errors = {}
    if quantity <= 0 or quantity > stock:
        errors["quantity"] = f"Must be greater than 0 and no more than your listed stock ({stock:g} {request.unit})."
    if quantity > request.quantity:
        errors["quantity"] = f"Cannot exceed the requested {request.quantity:g} {request.unit}."
    if earliest_delivery_date > request.delivery_deadline:
        errors["earliest_delivery_date"] = "Must be on or before the buyer's deadline."
    if valid_until < timezone.localdate():
        errors["valid_until"] = "Must not be in the past."
    if errors:
        raise ValidationError(errors)
    if request.quotations.filter(supplier=profile, inventory_item=inventory_item,
                                 status__in=Quotation.OPEN_STATUSES).exists():
        raise WorkflowError("You already have an open quotation for this listing; revise it instead.",
                            "duplicate_quotation")

    match = request.matches.filter(inventory_item=inventory_item).first()
    quotation = Quotation.objects.create(
        sourcing_request=request, match=match, supplier=profile, inventory_item=inventory_item,
        quantity=quantity, price_per_unit=price_per_unit, delivery_cost=delivery_cost,
        quality_grade=inventory_item.quality_grade, earliest_delivery_date=earliest_delivery_date,
        valid_until=valid_until, includes_delivery=includes_delivery, notes=notes,
    )
    quotation.record(user, "submitted", quantity=quantity, price_per_unit=price_per_unit,
                     delivery_cost=delivery_cost)
    quotation.save(update_fields=["history"])
    if match and match.status in {Match.Status.POTENTIAL, Match.Status.CONTACTED}:
        match.status = Match.Status.AVAILABILITY_CONFIRMED
        match.confirmed_quantity = quantity
        match.responded_at = timezone.now()
        match.save()
    if request.status in {SourcingRequest.Status.OPEN, SourcingRequest.Status.MATCHED}:
        request.status = SourcingRequest.Status.NEGOTIATING
        request.save(update_fields=["status", "updated_at"])
    notify(request.buyer, f"New quotation from {profile.business_name}",
           f"{quantity:g} {request.unit} at KES {price_per_unit:,} per {request.unit} for {request.reference}.",
           Notification.Kind.QUOTATION, f"/app/requests/{request.pk}/")
    return quotation


def _require_open_quote(quotation):
    if not quotation.is_open:
        raise WorkflowError(f"Quotation {quotation.reference} is no longer open.")
    _require_active(quotation.sourcing_request)


@transaction.atomic
def counter_quotation(buyer, quotation, price_per_unit=None, quantity=None, note=""):
    _require_buyer_owner(buyer, quotation.sourcing_request)
    _require_open_quote(quotation)
    if price_per_unit is None and quantity is None:
        raise ValidationError({"price_per_unit": "Propose a price and/or quantity."})
    quotation.status = Quotation.Status.COUNTERED
    quotation.counter_price_per_unit = price_per_unit
    quotation.counter_quantity = quantity
    quotation.counter_note = note
    quotation.record(buyer, "countered", price_per_unit=price_per_unit, quantity=quantity, note=note)
    quotation.save()
    notify(quotation.supplier.user, f"Counter-offer on {quotation.reference}",
           note or "The buyer proposed new terms.", Notification.Kind.QUOTATION, f"/app/quotations/{quotation.pk}/")
    return quotation


@transaction.atomic
def revise_quotation(user, quotation, price_per_unit=None, quantity=None, delivery_cost=None, note="",
                     accept_counter=False):
    profile = _supplier_profile(user)
    if quotation.supplier_id != profile.pk:
        raise WorkflowError("Not your quotation.", "permission_denied")
    _require_open_quote(quotation)
    if accept_counter:
        if quotation.status != Quotation.Status.COUNTERED:
            raise WorkflowError("There is no counter-offer to accept.")
        price_per_unit = quotation.counter_price_per_unit or quotation.price_per_unit
        quantity = quotation.counter_quantity or quotation.quantity
    if quantity is not None:
        quantity = Decimal(str(quantity))
        req = quotation.sourcing_request
        stock = Decimal(str(from_base_quantity(quotation.inventory_item.base_quantity, req.unit)))
        if quantity <= 0 or quantity > min(stock, req.quantity):
            raise ValidationError({"quantity": f"Must be between 0 and {min(stock, req.quantity):g}."})
        quotation.quantity = quantity
    if price_per_unit is not None:
        quotation.price_per_unit = price_per_unit
    if delivery_cost is not None:
        quotation.delivery_cost = delivery_cost
    quotation.status = Quotation.Status.SUBMITTED
    quotation.revision += 1
    quotation.counter_price_per_unit = quotation.counter_quantity = None
    quotation.record(user, "counter_accepted" if accept_counter else "revised",
                     price_per_unit=quotation.price_per_unit, quantity=quotation.quantity,
                     delivery_cost=quotation.delivery_cost, note=note)
    quotation.save()
    notify(quotation.sourcing_request.buyer, f"{profile.business_name} revised {quotation.reference}",
           note, Notification.Kind.QUOTATION, f"/app/requests/{quotation.sourcing_request_id}/")
    return quotation


@transaction.atomic
def accept_quotation(buyer, quotation):
    request = quotation.sourcing_request
    _require_buyer_owner(buyer, request)
    _require_open_quote(quotation)
    if quotation.status == Quotation.Status.COUNTERED:
        raise WorkflowError("Wait for the supplier to respond to your counter-offer before accepting.")
    accepted = request.accepted_quantity()
    if accepted + quotation.quantity > request.quantity:
        raise WorkflowError(
            f"Accepting this would exceed the requested quantity ({accepted:g} of {request.quantity:g} "
            f"{request.unit} already accepted).", "quantity_exceeded")
    quotation.status = Quotation.Status.ACCEPTED
    quotation.decided_at = timezone.now()
    quotation.record(buyer, "accepted")
    quotation.save()
    log_action(buyer, "quotation.accepted", quotation, total=str(quotation.total))
    notify(quotation.supplier.user, f"Your quotation {quotation.reference} was accepted",
           "The buyer will place an order; you'll be asked to reconfirm availability.",
           Notification.Kind.QUOTATION, f"/app/quotations/{quotation.pk}/")
    return quotation


@transaction.atomic
def reject_quotation(buyer, quotation, reason=""):
    _require_buyer_owner(buyer, quotation.sourcing_request)
    if quotation.status not in Quotation.OPEN_STATUSES:
        raise WorkflowError("Only open quotations can be rejected.")
    quotation.status = Quotation.Status.REJECTED
    quotation.decided_at = timezone.now()
    quotation.record(buyer, "rejected", reason=reason)
    quotation.save()
    notify(quotation.supplier.user, f"Quotation {quotation.reference} was not accepted", reason,
           Notification.Kind.QUOTATION, f"/app/quotations/{quotation.pk}/")
    return quotation


@transaction.atomic
def withdraw_quotation(user, quotation):
    profile = _supplier_profile(user)
    if quotation.supplier_id != profile.pk:
        raise WorkflowError("Not your quotation.", "permission_denied")
    if quotation.status not in Quotation.OPEN_STATUSES:
        raise WorkflowError("Only open quotations can be withdrawn.")
    quotation.status = Quotation.Status.WITHDRAWN
    quotation.record(user, "withdrawn")
    quotation.save()
    notify(quotation.sourcing_request.buyer, f"{profile.business_name} withdrew {quotation.reference}", "",
           Notification.Kind.QUOTATION, f"/app/requests/{quotation.sourcing_request_id}/")
    return quotation


# --- Offer comparison -------------------------------------------------------------

def compare_offers(request):
    """Side-by-side comparison of open and accepted offers, plus a cheapest plan.

    Each row reports landed cost (goods + delivery) per unit so offers with and
    without delivery are comparable.
    """
    rows = []
    quotes = (request.quotations.filter(status__in=[*Quotation.OPEN_STATUSES, Quotation.Status.ACCEPTED])
              .select_related("supplier", "match", "inventory_item__location"))
    for q in quotes:
        if q.status != Quotation.Status.ACCEPTED and not q.is_open:
            continue
        rows.append({
            "quotation": q,
            "landed_unit_cost": (q.total / q.quantity).quantize(Decimal("0.01")),
            "match_score": q.match.score if q.match else None,
            "distance_km": q.match.distance_km if q.match else None,
            "reliability": q.supplier.reliability_score,
            "average_rating": q.supplier.average_rating,
        })
    rows.sort(key=lambda r: (r["landed_unit_cost"], -(r["match_score"] or 0)))

    remaining, plan = request.quantity, []
    for r in rows:
        if remaining <= 0:
            break
        take = min(remaining, r["quotation"].quantity)
        plan.append({"quotation": r["quotation"], "quantity": take})
        remaining -= take
    plan_cost = sum((p["quotation"].total * p["quantity"] / p["quotation"].quantity for p in plan), Decimal(0))
    return {
        "offers": rows,
        "cheapest_plan": plan,
        "cheapest_plan_quantity": request.quantity - max(remaining, Decimal(0)),
        "cheapest_plan_cost": plan_cost.quantize(Decimal("0.01")),
        "within_budget": (plan_cost <= request.target_budget) if request.target_budget else None,
    }
