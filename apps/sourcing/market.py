"""Market check for farmers: is anyone buying this product, where, how much and at what price?

Buyers' identities are not shown here; a supplier sees who the buyer is once they are
matched to the request (their Opportunities page).
"""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.catalog.models import UNIT_TO_KG, InventoryItem, to_base_quantity
from apps.catalog.services import find_product
from apps.core.geo import haversine_km

from .matching import OPEN_FOR_OFFERS
from .models import Match, SourcingRequest

RECENT_DAYS = 90


@dataclass
class DemandRow:
    request: SourcingRequest
    distance_km: float | None
    budget_per_unit: Decimal | None
    my_match: Match | None
    i_can_supply: bool


@dataclass
class MarketReport:
    query: str
    product: object = None
    rows: list = field(default_factory=list)
    buyers: int = 0
    demand_kg: float = 0.0
    other_units: dict = field(default_factory=dict)
    sales_count: int = 0
    sales_kg: float = 0.0
    avg_price_kg: Decimal | None = None
    min_price_kg: Decimal | None = None
    max_price_kg: Decimal | None = None
    competitors: int = 0
    my_listings: list = field(default_factory=list)

    @property
    def level(self):
        if self.rows and (len(self.rows) >= 3 or self.buyers >= 2):
            return "strong"
        if self.rows:
            return "some"
        if self.sales_count:
            return "past"
        return "none"


def market_report(query, supplier=None):
    report = MarketReport(query=query)
    product = find_product(query)
    report.product = product
    if not product or not product.is_active:
        return report

    today = timezone.localdate()
    origin = supplier.location.coords if supplier and supplier.location_id else None
    reqs = (SourcingRequest.objects.filter(product=product, status__in=OPEN_FOR_OFFERS, delivery_deadline__gte=today)
            .select_related("delivery_location", "variety").order_by("delivery_deadline"))
    if supplier:
        reqs = reqs.exclude(buyer_id=supplier.user_id)
        report.my_listings = list(supplier.inventory.filter(product=product, is_active=True))
    my_matches = {}
    if supplier:
        for m in Match.objects.filter(supplier=supplier, sourcing_request__in=reqs).exclude(
                status__in=[Match.Status.DECLINED, Match.Status.EXPIRED]):
            my_matches.setdefault(m.sourcing_request_id, m)

    buyers = set()
    for r in reqs:
        buyers.add(r.buyer_id)
        if r.unit in UNIT_TO_KG:
            report.demand_kg += to_base_quantity(r.quantity, r.unit)
        else:
            report.other_units[r.get_unit_display()] = report.other_units.get(r.get_unit_display(), 0) + float(r.quantity)
        report.rows.append(DemandRow(
            request=r,
            distance_km=haversine_km(origin, r.delivery_location.coords) if origin else None,
            budget_per_unit=(r.target_budget / r.quantity).quantize(Decimal("0.01")) if r.target_budget else None,
            my_match=my_matches.get(r.pk),
            i_can_supply=any(item.quantity_available > 0 for item in report.my_listings)))
    report.buyers = len(buyers)

    from apps.orders.models import SaleRecord
    sales = SaleRecord.objects.filter(product_name__iexact=product.name,
                                      completed_at__gte=timezone.now() - timedelta(days=RECENT_DAYS))
    prices = []
    for s in sales:
        report.sales_count += 1
        if s.unit in UNIT_TO_KG:
            kg = to_base_quantity(s.quantity, s.unit)
            report.sales_kg += kg
            prices.append(s.price_per_unit / Decimal(str(UNIT_TO_KG[s.unit])))
    if prices:
        report.avg_price_kg = (sum(prices) / len(prices)).quantize(Decimal("0.01"))
        report.min_price_kg, report.max_price_kg = min(prices), max(prices)

    competitors = InventoryItem.objects.available().fresh().filter(product=product)
    if supplier:
        competitors = competitors.exclude(supplier=supplier)
    report.competitors = competitors.values("supplier").distinct().count()
    return report
