"""Browse-and-search for the Market page: products and people, filtered by category and county.

Farmers see what processors need (open demand by product) and the processors buying;
processors see what farmers have in stock (buyer-visible listings) and the farmers.
Only approved (verified) businesses are listed. Buyer identities are still never tied to
individual requests here: a farmer sees who posted a request once they're matched to it.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Q

from apps.accounts.models import BuyerProfile, SupplierProfile, VerificationStatus
from apps.catalog.models import UNIT_TO_KG, Category, InventoryItem, to_base_quantity
from apps.core.models import Location

from .matching import OPEN_FOR_OFFERS
from .models import SourcingRequest

LIMIT = 60


@dataclass
class Filters:
    q: str = ""
    category: Category | None = None
    county: str = ""

    @classmethod
    def from_request(cls, params):
        q = params.get("q", "").strip()[:80]
        category = Category.objects.filter(pk=params.get("category")).first() if params.get("category", "").isdigit() else None
        county = params.get("county", "").strip()
        if county and not Location.objects.filter(county=county).exists():
            county = ""
        return cls(q=q, category=category, county=county)

    @property
    def active(self):
        return bool(self.q or self.category or self.county)


def categories():
    return Category.objects.filter(products__is_active=True).distinct().order_by("name")


def counties():
    return Location.objects.order_by("county").values_list("county", flat=True).distinct()


def _words(q):
    return [w for w in q.split() if len(w) >= 2][:5]


# --- products ---------------------------------------------------------------------------
@dataclass
class DemandRow:
    product: object
    requests: int = 0
    buyers: set = field(default_factory=set)
    kg: float = 0.0
    other_units: dict = field(default_factory=dict)
    counties: set = field(default_factory=set)
    earliest: object = None


def demand_by_product(f):
    """Open buyer demand grouped by product (for farmers)."""
    qs = (SourcingRequest.objects.filter(status__in=OPEN_FOR_OFFERS)
          .select_related("product__category", "delivery_location"))
    if f.category:
        qs = qs.filter(product__category=f.category)
    if f.county:
        qs = qs.filter(delivery_location__county=f.county)
    for w in _words(f.q):
        qs = qs.filter(Q(product__name__icontains=w) | Q(variety__name__icontains=w)
                       | Q(intended_application__icontains=w) | Q(product__category__name__icontains=w))
    rows = {}
    for r in qs:
        row = rows.setdefault(r.product_id, DemandRow(product=r.product))
        row.requests += 1
        row.buyers.add(r.buyer_id)
        if r.unit in UNIT_TO_KG:
            row.kg += to_base_quantity(r.quantity, r.unit)
        else:  # crates, pieces: sizes vary, so keep them in their own unit
            row.other_units[r.get_unit_display()] = row.other_units.get(r.get_unit_display(), 0) + float(r.quantity)
        row.counties.add(r.delivery_location.county)
        row.earliest = min(row.earliest, r.delivery_deadline) if row.earliest else r.delivery_deadline
    return sorted(rows.values(), key=lambda r: (-r.requests, -r.kg, r.product.name))


def visible_listings(f):
    """Stock buyers can see (approved supplier, fresh, available, with photos) — for processors."""
    qs = (InventoryItem.objects.available().fresh().with_required_photos()
          .filter(supplier__verification_status=VerificationStatus.APPROVED, product__is_active=True)
          .select_related("product__category", "variety", "location", "supplier__user")
          .prefetch_related("photos"))
    if f.category:
        qs = qs.filter(product__category=f.category)
    if f.county:
        qs = qs.filter(location__county=f.county)
    for w in _words(f.q):
        qs = qs.filter(Q(product__name__icontains=w) | Q(variety__name__icontains=w)
                       | Q(supplier__business_name__icontains=w) | Q(product__category__name__icontains=w)
                       | Q(location__county__icontains=w))
    return list(qs.order_by("product__name", "price_per_unit")[:LIMIT])


# --- people -----------------------------------------------------------------------------
@dataclass
class Person:
    profile: object
    products: list
    open_requests: int = 0
    listings: int = 0


def processors(f):
    """Verified processors / buyers and the products they buy (for farmers)."""
    qs = (BuyerProfile.objects.filter(verification_status=VerificationStatus.APPROVED)
          .select_related("user", "location"))
    if f.county:
        qs = qs.filter(location__county=f.county)
    if f.category:
        qs = qs.filter(user__sourcing_requests__product__category=f.category)
    for w in _words(f.q):
        qs = qs.filter(Q(business_name__icontains=w) | Q(business_type__icontains=w)
                       | Q(user__first_name__icontains=w) | Q(user__last_name__icontains=w)
                       | Q(location__county__icontains=w) | Q(user__sourcing_requests__product__name__icontains=w))
    profiles = list(qs.distinct().order_by("business_name")[:LIMIT])
    buys, open_counts = defaultdict(set), defaultdict(int)
    for buyer_id, name, status in (SourcingRequest.objects.filter(buyer__in=[p.user_id for p in profiles])
                                   .values_list("buyer_id", "product__name", "status")):
        buys[buyer_id].add(name)
        open_counts[buyer_id] += status in OPEN_FOR_OFFERS
    return [Person(profile=p, products=sorted(buys[p.user_id]), open_requests=open_counts[p.user_id])
            for p in profiles]


def farmers(f):
    """Verified farmers and what they currently list (for processors)."""
    qs = (SupplierProfile.objects.filter(verification_status=VerificationStatus.APPROVED)
          .select_related("user", "location"))
    if f.county:
        qs = qs.filter(location__county=f.county)
    if f.category:
        qs = qs.filter(inventory__product__category=f.category, inventory__is_active=True)
    for w in _words(f.q):
        qs = qs.filter(Q(business_name__icontains=w) | Q(user__first_name__icontains=w)
                       | Q(user__last_name__icontains=w) | Q(location__county__icontains=w)
                       | Q(inventory__product__name__icontains=w))
    profiles = list(qs.distinct().order_by("business_name")[:LIMIT])
    stock = defaultdict(set)
    counts = defaultdict(int)
    for item in (InventoryItem.objects.available().fresh().with_required_photos()
                 .filter(supplier__in=profiles).select_related("product")):
        stock[item.supplier_id].add(item.product.name)
        counts[item.supplier_id] += 1
    return [Person(profile=p, products=sorted(stock[p.pk]), listings=counts[p.pk]) for p in profiles]
