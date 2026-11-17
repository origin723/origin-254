"""Supplier matching engine.

Given a sourcing request, find approved suppliers whose current inventory can
serve it, score each candidate on six weighted criteria, rank them, and plan a
multi-supplier allocation when no single supplier can cover the full quantity.

Criteria (each scored 0..1, weights from core.MatchingConfig):
  product      exact variety = 1.0, unspecified variety = 0.8, other variety = 0.6
  quantity     share of the requested quantity the listing can cover
  quality      grade meets requirement = 1.0 (one grade below = 0.4 if allowed),
               minus 0.25 for each declared spec (min_*/max_*) not met
  proximity    1 - distance / max_distance_km
  delivery     arrangement satisfiable = 1.0 otherwise 0.3. Listings that
               cannot reach the buyer before the deadline are excluded.
  reliability  supplier's track record (or the new-supplier default)

Hard filters: same product, supplier APPROVED and active, inventory active,
fresh (reconfirmed within INVENTORY_STALE_DAYS), at least MIN_PHOTOS_PER_LISTING
product photos, positive quantity, within
max_distance_km, deliverable before the deadline, quality not too low.
"""
import math
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import VerificationStatus
from apps.catalog.models import GRADE_RANK, InventoryItem, from_base_quantity, units_compatible
from apps.core.geo import haversine_km, location_ids_within
from apps.core.models import MatchingConfig

from .models import DeliveryArrangement, Match, SourcingRequest


@dataclass
class Candidate:
    item: InventoryItem
    distance_km: float
    components: dict
    score: float
    available_quantity: float  # in the request's unit
    notes: list = field(default_factory=list)
    suggested_quantity: float = 0.0


@dataclass
class MatchResult:
    request: SourcingRequest
    candidates: list
    excluded: dict
    requested_quantity: float
    planned_quantity: float

    @property
    def fully_covered(self):
        return self.planned_quantity + 1e-9 >= self.requested_quantity

    @property
    def coverage(self):
        return min(1.0, self.planned_quantity / self.requested_quantity) if self.requested_quantity else 0.0

    @property
    def suppliers_needed(self):
        return len([c for c in self.candidates if c.suggested_quantity > 0])


def _to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def quality_score(request, item, config):
    """Return (score, notes) or (None, reason) if the listing must be excluded."""
    diff = GRADE_RANK[item.quality_grade] - request.required_grade_rank
    if diff >= 0:
        score, notes = 1.0, []
    elif diff == -1 and config.allow_one_grade_below:
        score, notes = 0.4, [f"Grade {item.get_quality_grade_display()} is one below the requested grade"]
    else:
        return None, "quality grade below requirement"

    # Declared numeric specifications such as {"min_size_mm": 70, "max_moisture_pct": 13}
    for key, required in (request.quality_specifications or {}).items():
        required_num = _to_number(required)
        if required_num is None or not (key.startswith("min_") or key.startswith("max_")):
            continue
        attr = key[4:]
        actual = _to_number((item.quality_attributes or {}).get(attr))
        if actual is None:
            notes.append(f"{attr} not declared by supplier")
            score -= 0.1
        elif key.startswith("min_") and actual < required_num:
            notes.append(f"{attr} {actual} below minimum {required_num}")
            score -= 0.25
        elif key.startswith("max_") and actual > required_num:
            notes.append(f"{attr} {actual} above maximum {required_num}")
            score -= 0.25
    return max(score, 0.0), notes


def product_score(request, item):
    if not request.variety_id or item.variety_id == request.variety_id:
        return 1.0
    if item.variety_id is None:
        return 0.8
    return 0.6


def delivery_assessment(request, item, distance_km, config):
    """Return (score, notes) or (None, reason) when the deadline cannot be met."""
    today = timezone.localdate()
    transit_days = math.ceil(distance_km / max(config.transport_km_per_day, 1))
    ready = max(today, item.available_from)
    eta = ready + timedelta(days=transit_days)
    if eta > request.delivery_deadline:
        return None, "cannot deliver before the deadline"

    supplier = item.supplier
    notes = [f"Estimated arrival {eta:%d %b} ({transit_days} day transit)"]
    if request.delivery_arrangement == DeliveryArrangement.SUPPLIER_DELIVERS:
        delivers = item.can_deliver and supplier.can_deliver and supplier.delivery_radius_km >= distance_km
        if not delivers:
            notes.append("Supplier does not deliver this distance; collection or platform logistics needed")
            return 0.3, notes
    return 1.0, notes


def find_candidates(request, config=None, item_ids=None):
    config = config or MatchingConfig.get()
    weights = config.weights()
    origin = request.delivery_location.coords
    excluded = {}

    nearby = location_ids_within(*origin, config.max_distance_km)
    items = (InventoryItem.objects.available().fresh().with_required_photos()
             .filter(product=request.product,
                     supplier__verification_status=VerificationStatus.APPROVED,
                     supplier__user__is_active=True,
                     location_id__in=nearby,
                     available_from__lte=request.delivery_deadline)
             .exclude(supplier__user=request.buyer)
             .select_related("supplier", "supplier__user", "location", "variety"))
    if item_ids is not None:
        items = items.filter(pk__in=item_ids)

    requested_base = request.base_quantity
    candidates = []
    for item in items:
        if not units_compatible(item.unit, request.unit):
            excluded[item.pk] = f"listed in {item.get_unit_display().lower()}, not comparable to {request.get_unit_display().lower()}"
            continue
        distance = haversine_km(origin, item.location.coords)
        q_score, q_notes = quality_score(request, item, config)
        if q_score is None:
            excluded[item.pk] = q_notes
            continue
        d_score, d_notes = delivery_assessment(request, item, distance, config)
        if d_score is None:
            excluded[item.pk] = d_notes
            continue
        components = {
            "product": product_score(request, item),
            "quantity": min(1.0, item.base_quantity / requested_base),
            "quality": q_score,
            "proximity": max(0.0, 1 - distance / config.max_distance_km),
            "delivery": d_score,
            "reliability": float(item.supplier.effective_reliability(config.new_supplier_reliability)),
        }
        score = sum(weights[k] * v for k, v in components.items())
        if score < float(config.min_score):
            excluded[item.pk] = f"score {score:.2f} below threshold"
            continue
        candidates.append(Candidate(
            item=item, distance_km=distance, components=components, score=score,
            available_quantity=from_base_quantity(item.base_quantity, request.unit),
            notes=q_notes + d_notes,
        ))

    candidates.sort(key=lambda c: (-c.score, c.distance_km))
    return candidates[: config.max_matches], excluded


def plan_allocation(request, candidates):
    """Greedy allocation in rank order until the requested quantity is covered."""
    remaining = float(request.quantity)
    for c in candidates:
        take = min(remaining, c.available_quantity)
        c.suggested_quantity = max(take, 0.0)
        remaining -= c.suggested_quantity
    return float(request.quantity) - max(remaining, 0.0)


def _dec(value, places="0.01"):
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


@transaction.atomic
def run_matching(request: SourcingRequest) -> MatchResult:
    """Score, rank and persist matches for a request. Safe to re-run.

    Matches the supplier has already engaged with (notified, confirmed,
    committed, declined) are kept and re-scored; untouched potential matches
    that no longer qualify are removed.
    """
    config = MatchingConfig.get()
    candidates, excluded = find_candidates(request, config)
    planned = plan_allocation(request, candidates)

    keep_ids = []
    for rank, c in enumerate(candidates, start=1):
        match, _ = Match.objects.update_or_create(
            sourcing_request=request, inventory_item=c.item,
            defaults={
                "supplier": c.item.supplier, "rank": rank, "score": _dec(c.score, "0.0001"),
                "score_breakdown": {"components": {k: round(v, 3) for k, v in c.components.items()},
                                    "weights": {k: round(v, 3) for k, v in config.weights().items()},
                                    "notes": c.notes},
                "distance_km": _dec(c.distance_km, "0.1"),
                "available_quantity": _dec(c.available_quantity),
                "suggested_quantity": _dec(c.suggested_quantity),
            },
        )
        keep_ids.append(match.pk)

    stale = request.matches.exclude(pk__in=keep_ids)
    stale.filter(status=Match.Status.POTENTIAL).delete()
    stale.filter(status__in=[Match.Status.CONTACTED, Match.Status.AVAILABILITY_CONFIRMED]).update(
        status=Match.Status.EXPIRED, rank=0)

    request.last_matched_at = timezone.now()
    fields = ["last_matched_at", "updated_at"]
    if candidates and request.status == SourcingRequest.Status.OPEN:
        request.status = SourcingRequest.Status.MATCHED
        fields.append("status")
    request.save(update_fields=fields)

    return MatchResult(request=request, candidates=candidates, excluded=excluded,
                       requested_quantity=float(request.quantity), planned_quantity=planned)




# --- Nearest-supplier suggestions --------------------------------------------------

ROAD_FACTOR = 1.3   # Kenyan roads are on average ~30% longer than the straight line
AVG_SPEED_KMH = 50  # loaded truck incl. rough roads and stops


@dataclass
class NearbySupplier:
    item: InventoryItem
    distance_km: float

    @property
    def road_km(self):
        return round(self.distance_km * ROAD_FACTOR)

    @property
    def travel_hours(self):
        return round(self.road_km / AVG_SPEED_KMH, 1)

    @property
    def supplier(self):
        return self.item.supplier


OPEN_FOR_OFFERS = (SourcingRequest.Status.OPEN, SourcingRequest.Status.MATCHED, SourcingRequest.Status.NEGOTIATING)


@transaction.atomic
def offer_new_stock(item):
    """Add a newly listed or updated listing to open requests it qualifies for.

    Existing matches are left exactly as they are (no re-ranking, nothing expired):
    the listing is appended after them and the buyer is told a new supplier is available.
    Returns the matches created.
    """
    from apps.comms.models import Notification
    from apps.comms.notify import notify

    config = MatchingConfig.get()
    created = []
    requests = (SourcingRequest.objects.filter(product_id=item.product_id, status__in=OPEN_FOR_OFFERS,
                                               delivery_deadline__gte=timezone.localdate())
                .exclude(buyer_id=item.supplier.user_id).exclude(matches__inventory_item=item)
                .select_related("delivery_location", "buyer"))
    for request in requests:
        candidates, _ = find_candidates(request, config, item_ids=[item.pk])
        if not candidates:
            continue
        c = candidates[0]
        live = request.matches.exclude(status__in=[Match.Status.DECLINED, Match.Status.EXPIRED])
        planned = sum(float(m.suggested_quantity) for m in live)
        remaining = max(float(request.quantity) - planned, 0.0)
        last_rank = max((m.rank for m in request.matches.all()), default=0)
        match = Match.objects.create(
            sourcing_request=request, inventory_item=item, supplier=item.supplier, rank=last_rank + 1,
            score=_dec(c.score, "0.0001"),
            score_breakdown={"components": {k: round(v, 3) for k, v in c.components.items()},
                             "weights": {k: round(v, 3) for k, v in config.weights().items()},
                             "notes": c.notes + ["Added when the supplier listed new stock"]},
            distance_km=_dec(c.distance_km, "0.1"), available_quantity=_dec(c.available_quantity),
            suggested_quantity=_dec(min(remaining, c.available_quantity)))
        if request.status == SourcingRequest.Status.OPEN:
            request.status = SourcingRequest.Status.MATCHED
            request.save(update_fields=["status", "updated_at"])
        notify(request.buyer, f"New supplier for {request.reference}: {item.supplier.business_name}",
               f"{c.available_quantity:g} {request.unit} of {item.product.name} available, "
               f"{c.distance_km:.0f} km away.", Notification.Kind.MATCH, f"/app/requests/{request.pk}/")
        created.append(match)
    return created


def nearest_suppliers(product, origin, variety=None, min_quantity=None, min_grade=None, radius_km=None, limit=15):
    """Approved suppliers with current stock of `product`, nearest first.

    `origin` is a core.Location (the buyer's delivery point). Distances are
    straight-line (haversine / PostGIS); `road_km` is an estimate.
    """
    config = MatchingConfig.get()
    radius = radius_km or config.max_distance_km
    nearby = location_ids_within(*origin.coords, radius)
    items = (InventoryItem.objects.available().fresh().with_required_photos()
             .filter(product=product, supplier__verification_status=VerificationStatus.APPROVED,
                     supplier__user__is_active=True, location_id__in=nearby)
             .select_related("supplier__user", "location", "variety").prefetch_related("photos"))
    if variety:
        items = items.filter(variety=variety)
    results = []
    for item in items:
        if min_grade and GRADE_RANK[item.quality_grade] < GRADE_RANK[min_grade]:
            continue
        if min_quantity and item.quantity_available < min_quantity:
            continue
        results.append(NearbySupplier(item=item, distance_km=haversine_km(origin.coords, item.location.coords)))
    results.sort(key=lambda r: r.distance_km)
    return results[:limit]


def nearest_first_plan(request, matches):
    """Cover the requested quantity using the closest matched suppliers first."""
    remaining, plan = float(request.quantity), []
    for m in sorted(matches, key=lambda m: m.distance_km):
        if remaining <= 0:
            break
        if m.status in {Match.Status.DECLINED, Match.Status.EXPIRED}:
            continue
        take = min(remaining, float(m.available_quantity))
        if take > 0:
            plan.append((m, take))
            remaining -= take
    covered = float(request.quantity) - max(remaining, 0)
    furthest = max((float(m.distance_km) for m, _ in plan), default=0)
    return {"plan": plan, "covered": covered, "furthest_km": furthest,
            "fully_covered": remaining <= 1e-9}
