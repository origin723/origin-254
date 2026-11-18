"""Fill every account's tabs with activity, through the real service layer.

    python manage.py seed_activity            # idempotent: safe to re-run

For every approved, active farmer:
  * at least 5 listings (fresh, with 2+ photos), adding Kenyan products as needed;
  * three trades with nearby approved processors:
      completed   request → quote → order → paid → inspected → delivered by a logistics
                  partner → completed, sale recorded, payout, reviews both ways
      in transit  the same up to "in transit", collected by the buyer (Deliveries / Orders / Payments)
      quoted      a quotation waiting for the buyer (Quotations tab)
  * a message thread with each buyer.
Requests also match other farmers, so they get Opportunities too. Run seed_kenya first
(it provides the product photos and catalogue).
"""
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import BuyerProfile, LogisticsProfile, SupplierProfile, User, VerificationStatus
from apps.catalog.models import InventoryItem, Product, ProductVariety, QualityGrade
from apps.comms.services import post_message, start_conversation
from apps.core.exceptions import WorkflowError
from apps.core.geo import haversine_km
from apps.core.models import MatchingConfig
from apps.orders import services as orders
from apps.orders.models import Delivery, Inspection, OrderLine
from apps.reviews.services import create_review
from apps.sourcing import services as sourcing
from apps.sourcing.models import DeliveryArrangement, SourcingRequest

from .seed_kenya import CATALOGUE, Command as SeedKenya

TAG = "[Activity demo]"
MIN_LISTINGS = 5
STAGES = ("completed", "in_transit", "quoted")
PRICE = {"Mango": 45, "Avocado": 65, "Pineapple": 50, "Passion fruit": 120, "Banana": 30, "Tomato": 70,
         "Onion": 90, "Sukuma wiki": 30, "Cabbage": 25, "Carrot": 50, "Irish potato": 45, "Sweet potato": 40,
         "Cassava": 30, "Maize": 45, "Sorghum": 50, "Rice": 150, "Beans": 120, "Green grams": 140,
         "Groundnuts": 160, "Macadamia": 190, "Coffee": 85, "Beef": 560}
USES = {"completed": "Weekly production run", "in_transit": "Restocking for next week",
        "quoted": "Trial batch for a new product line"}


class Command(BaseCommand):
    help = "Give every farmer 5+ products and every account orders, deliveries, payments, quotes and messages."

    def handle(self, *args, **opts):
        self.today = timezone.localdate()
        self.now = timezone.now()
        self.config = MatchingConfig.get()
        self.photo_helper = self._photo_helper()
        self.admin = User.objects.filter(role="admin", is_active=True).order_by("pk").first()
        self.logistics = list(LogisticsProfile.objects.filter(verification_status=VerificationStatus.APPROVED,
                                                              user__is_active=True).select_related("user"))
        self.buyers = list(BuyerProfile.objects.filter(verification_status=VerificationStatus.APPROVED,
                                                       user__is_active=True).select_related("user", "location")
                           .order_by("pk"))
        farmers = list(SupplierProfile.objects.filter(verification_status=VerificationStatus.APPROVED,
                                                      user__is_active=True).select_related("user", "location")
                       .order_by("pk"))
        self.stdout.write(f"{len(farmers)} farmers, {len(self.buyers)} processors, {len(self.logistics)} logistics")

        counts = {"listings added": 0, **{s: 0 for s in STAGES}, "skipped": 0}
        self.k = 0  # rotates processors across farmers
        self.lk = 0  # rotates logistics partners across deliveries
        for farmer in farmers:
            counts["listings added"] += self._ensure_listings(farmer)
            for stage in STAGES:
                result = self._trade(farmer, stage)
                counts[result] = counts.get(result, 0) + 1
        counts["processor top-ups"] = self._top_up_processors(farmers)
        self.stdout.write(self.style.SUCCESS("Done: " + ", ".join(f"{k} {v}" for k, v in counts.items())))
        self._report(farmers)

    # --- listings ---------------------------------------------------------------------
    def _photo_helper(self):
        helper = SeedKenya()
        helper.photos_dir = Path(settings.PROJECT_ROOT) / "demo_assets" / "product_photos"
        credits = helper.photos_dir / "credits.json"
        helper.credits = json.loads(credits.read_text()) if credits.exists() else {}
        return helper

    def _ensure_listings(self, farmer):
        added = 0
        items = list(InventoryItem.objects.filter(supplier=farmer).select_related("product"))
        with transaction.atomic():
            for item in items:  # keep existing stock matchable: fresh, available, photographed
                InventoryItem.objects.filter(pk=item.pk).update(
                    last_confirmed_at=self.now, is_active=True,
                    available_until=max(item.available_until or self.today, self.today + timedelta(days=30)))
        for n, item in enumerate(items):
            self.photo_helper._photos(item, CATALOGUE.get(item.product.name, (None, "x"))[1], start=n)
        have = {i.product.name for i in items if i.is_active}
        choices = [p for p in PRICE if p not in have]
        start = farmer.pk % len(choices) if choices else 0
        for product_name in (choices[start:] + choices[:start])[:max(MIN_LISTINGS - len(have), 0)]:
            product = Product.objects.filter(name=product_name).first()
            if not product:
                continue
            variety = ProductVariety.objects.filter(product=product).order_by("pk").first()
            with transaction.atomic():
                item = InventoryItem.objects.create(
                    supplier=farmer, product=product, variety=variety, unit="kg",
                    quantity_available=2000 + (farmer.pk * 137 + product.pk * 61) % 4000,
                    quality_grade=QualityGrade.GRADE_1, price_per_unit=PRICE[product_name],
                    location=farmer.location, harvest_date=self.today - timedelta(days=2),
                    available_from=self.today, available_until=self.today + timedelta(days=30),
                    can_deliver=True, packaging="Crates / bags as agreed", is_active=True,
                    quality_attributes={"other_details": f"{TAG} Sorted and graded {product_name.lower()}."},
                    last_confirmed_at=self.now)
            self.photo_helper._photos(item, CATALOGUE[product_name][1], start=len(have) + added)
            added += 1
        return added

    # --- trades -----------------------------------------------------------------------
    def _buyer_for(self, farmer):
        """Next processor in rotation that is within matching range of this farmer."""
        n = len(self.buyers)
        for step in range(n):
            buyer = self.buyers[(self.k + step) % n]
            if buyer.user_id == farmer.user_id:
                continue
            if haversine_km(buyer.location.coords, farmer.location.coords) <= self.config.max_distance_km * 0.9:
                self.k += step + 1
                return buyer
        return None

    def _listing_for(self, farmer, stage):
        items = list(InventoryItem.objects.available().fresh().with_required_photos()
                     .filter(supplier=farmer, unit="kg").select_related("product", "variety").order_by("pk"))
        return items[STAGES.index(stage) % len(items)] if items else None

    def _top_up_processors(self, farmers):
        """Every processor gets at least one completed order (purchase history, payments) with the
        nearest farmer in range that it hasn't already traded with in this pass."""
        done = 0
        for buyer in self.buyers:
            if OrderLine.objects.filter(order__buyer=buyer.user, status=OrderLine.Status.COMPLETED).exists():
                continue
            nearby = sorted((haversine_km(buyer.location.coords, f.location.coords), f.pk, f) for f in farmers
                            if f.user_id != buyer.user_id)
            for distance, _, farmer in nearby:
                if distance > self.config.max_distance_km * 0.9:
                    break
                if self._trade(farmer, "completed", buyer=buyer) == "completed":
                    done += 1
                    break
        return done

    def _trade(self, farmer, stage, buyer=None):
        marker = f"{TAG} {farmer.user.username} {stage}" + (f" for {buyer.user.username}" if buyer else "")
        if SourcingRequest.objects.filter(quality_notes__startswith=f"{marker}:").exists():
            return "existing"
        item, buyer = self._listing_for(farmer, stage), buyer or self._buyer_for(farmer)
        if not item or not buyer:
            self.stdout.write(f"  {farmer.business_name}: no {'kg listing' if not item else 'processor in range'} "
                              f"for {stage}")
            return "skipped"
        qty = Decimal(min(int(item.quantity_available * Decimal("0.25")), 1500) or 1)
        try:
            with transaction.atomic():
                req, _ = sourcing.create_sourcing_request(
                    buyer.user, product=item.product, variety=item.variety, quantity=qty, unit="kg",
                    delivery_location=buyer.location, delivery_deadline=self.today + timedelta(days=12),
                    min_quality_grade=item.quality_grade, quality_specifications={},
                    quality_notes=f"{marker}: clean, sorted produce.", intended_application=USES[stage],
                    packaging_requirements="Crates or bags", target_budget=qty * (item.price_per_unit or 50) * 2,
                    delivery_arrangement=DeliveryArrangement.BUYER_COLLECTS)
                match = req.matches.filter(inventory_item=item).first()
                if match is None:
                    raise WorkflowError(f"{farmer.business_name} was not matched to {req.reference}")
                sourcing.respond_to_match(farmer.user, match, available=True, quantity=qty)
                quote = sourcing.submit_quotation(
                    farmer.user, req, item, qty, item.price_per_unit or Decimal(50),
                    self.today + timedelta(days=2), self.today + timedelta(days=7),
                    delivery_cost=Decimal(0), includes_delivery=False, notes="Ready for collection.")
                convo = start_conversation(buyer.user, farmer.user, f"{req.reference} — {item.product.name}",
                                           sourcing_request=req,
                                           first_message=f"Hello, is your {item.product.name.lower()} still available?")
                post_message(farmer.user, convo, "Yes, freshly harvested. I've sent you a quotation.")
                if stage != "quoted":
                    self._order(buyer, farmer, req, quote, stage)
            return stage
        except (WorkflowError, ValidationError) as exc:
            self.stdout.write(f"  {farmer.business_name} ({stage}): {exc}")
            return "skipped"

    def _order(self, buyer, farmer, req, quote, stage):
        from apps.payments.services import start_payment

        sourcing.accept_quotation(buyer.user, quote)
        order = orders.create_order(buyer.user, req, [quote], notes=TAG)
        line = order.lines.select_related("delivery").get()
        orders.reconfirm_line(farmer.user, line, available=True, note="Stock reserved")
        order.refresh_from_db()
        if settings.PAYMENT_PROVIDER == "simulated":
            try:
                with transaction.atomic():
                    start_payment(buyer.user, order, buyer.user.phone or "0712345678")
            except ValidationError:  # saved phone isn't a valid Kenyan mobile: use a test number
                start_payment(buyer.user, order, "0712345678")
        if self.admin:
            orders.record_inspection(self.admin, line, Inspection.Stage.PRE_DISPATCH, Inspection.Result.PASSED,
                                     grade_observed=req.min_quality_grade, measurements={},
                                     accepted_quantity=line.quantity, notes="Meets the agreed grade.")
        d = Delivery.objects.get(order_line=line)
        actor = buyer.user  # in-transit: buyer collects (no partner, so it shows on the farmer's Deliveries tab)
        if self.logistics and stage == "completed":  # completed ones by a logistics partner
            partner = self.logistics[self.lk % len(self.logistics)].user
            self.lk += 1
            orders.assign_logistics(buyer.user, d, partner, scheduled_date=self.today + timedelta(days=1))
            actor = partner
        d.refresh_from_db()
        if d.status == Delivery.Status.PENDING:
            orders.update_delivery(actor, d, Delivery.Status.SCHEDULED)
        orders.update_delivery(actor, d, Delivery.Status.PICKED_UP, vehicle_reg="KDB 254K",
                               driver_name="Otieno", note="Loaded at the farm")
        orders.update_delivery(actor, d, Delivery.Status.IN_TRANSIT, note="On the way")
        if stage == "in_transit":
            return
        orders.update_delivery(actor, d, Delivery.Status.DELIVERED, received_quantity=line.quantity,
                               received_by=f"{buyer.user.first_name or 'Stores'} (stores)", note="Offloaded")
        line.refresh_from_db()
        orders.complete_line(buyer.user, line, note="Received in good condition")
        line = OrderLine.objects.get(pk=line.pk)
        create_review(buyer.user, line, rating=5, comment="Good quality, as described.",
                      quality_rating=5, timeliness_rating=4)
        create_review(farmer.user, line, rating=5, comment="Clear communication, paid promptly.")

    # --- summary ----------------------------------------------------------------------
    def _report(self, farmers):
        thin = [f.business_name for f in farmers
                if InventoryItem.objects.filter(supplier=f, is_active=True).count() < MIN_LISTINGS]
        no_orders = [b.business_name for b in self.buyers if not OrderLine.objects.filter(
            order__buyer=b.user, status=OrderLine.Status.COMPLETED).exists()]
        no_jobs = [lp.business_name for lp in self.logistics
                   if not Delivery.objects.filter(logistics_partner=lp.user).exists()]
        self.stdout.write(f"Farmers with < {MIN_LISTINGS} listings: {thin or 'none'}")
        self.stdout.write(f"Processors without a completed order: {no_orders or 'none'}")
        self.stdout.write(f"Logistics partners without delivery jobs: {no_jobs or 'none'}")
