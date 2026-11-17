"""MVP demonstration: a Nairobi jam maker sources 1,500 kg of mangoes.

    python manage.py mango_demo                 # full scenario, end to end
    python manage.py mango_demo --stop-after quotes   # leave it mid-way to continue in the web app

Runs the real service layer (the same code the API and web app call) and
narrates each of the 11 steps in the specification.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Product, QualityGrade
from apps.comms.services import post_message, start_conversation
from apps.core.models import Location
from apps.orders import services as orders
from apps.orders.models import Delivery, Inspection
from apps.reviews.services import create_review
from apps.sourcing import services as sourcing
from apps.sourcing.models import DeliveryArrangement

STAGES = ["request", "matching", "contact", "quotes", "agreement", "inspection", "delivery", "complete"]


class Command(BaseCommand):
    help = "Run the 1,500 kg mango jam sourcing scenario."

    def add_arguments(self, parser):
        parser.add_argument("--stop-after", choices=STAGES, default="complete")

    def say(self, step, text):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n[{step}] {text}"))

    def info(self, text):
        self.stdout.write(f"    {text}")

    @transaction.atomic
    def handle(self, *args, **opts):
        stop = opts["stop_after"]
        call_command("seed_demo", reset=True, stdout=self.stdout)
        U = lambda name: User.objects.get(username=name)  # noqa: E731
        buyer, qa, logi = U("mama_jam"), U("qa_officer"), U("swift_logistics")
        today = timezone.localdate()

        # 1. Register the sourcing request
        self.say(1, "Mama Jam Foods (Nairobi) submits a sourcing request")
        req, result = sourcing.create_sourcing_request(
            buyer, product=Product.objects.get(name="Mango"), quantity=Decimal("1500"), unit="kg",
            delivery_location=Location.objects.get(name="Nairobi Industrial Area"), delivery_deadline=today + timedelta(days=7),
            min_quality_grade=QualityGrade.GRADE_1, quality_specifications={},
            quality_notes="Fully ripe, firm flesh, no anthracnose", intended_application="Mango jam production",
            packaging_requirements="Ventilated plastic crates, max 20 kg each", target_budget=Decimal("80000"),
            delivery_arrangement=DeliveryArrangement.SUPPLIER_DELIVERS)
        self.info(f"Request {req.reference}: 1,500 kg mango, grade 1+, by {req.delivery_deadline:%d %b}")

        # 2-5. Search, check, identify, rank
        self.say("2-5", "Matching engine searches approved suppliers, checks quantity, quality, location and "
                        "delivery, then ranks")
        for m in req.matches.order_by("rank"):
            c = m.score_breakdown["components"]
            self.info(f"#{m.rank} {m.supplier.business_name:<40} score {m.score:.3f}  {m.distance_km:>5} km  "
                      f"{m.available_quantity:>6g} kg  suggest {m.suggested_quantity:>6g} kg  "
                      f"(qty {c['quantity']:.2f} qual {c['quality']:.2f} prox {c['proximity']:.2f} "
                      f"deliv {c['delivery']:.2f} rel {c['reliability']:.2f})")
        self.info(f"Coverage {result.coverage:.0%} using {result.suppliers_needed} supplier(s); "
                  f"{len(result.excluded)} listing(s) excluded:")
        from apps.catalog.models import InventoryItem
        for item_id, reason in result.excluded.items():
            self.info(f"   – {InventoryItem.objects.get(pk=item_id).supplier.business_name}: {reason}")
        self.info("   – Kitui Dryland Fruits: not shown at all (verification pending)")
        self.info("   – Meru Sunrise Farm: not shown at all (stock not reconfirmed for 30 days)")
        if stop == "matching":
            return self._done(req)

        # 6. Contact suppliers
        self.say(6, "Buyer notifies matched suppliers and messages one directly")
        notified = sourcing.notify_matched_suppliers(buyer, req)
        self.info(f"{len(notified)} supplier(s) asked to confirm availability (in-app + email + SMS)")
        top = req.matches.order_by("rank").first()
        convo = start_conversation(buyer, top.supplier.user, f"{req.reference} — Mango", sourcing_request=req,
                                   first_message="Hello! Can you supply ripe Apple mangoes in 20 kg crates?")
        post_message(top.supplier.user, convo, "Yes, harvested this week. We'll send a quotation today.")
        self.info(f"Conversation with {top.supplier.business_name}: {convo.messages.count()} messages")
        if stop == "contact":
            return self._done(req)

        # 7. Suppliers submit quotations; negotiation
        self.say(7, "Suppliers confirm availability and submit quotations; buyer negotiates")
        quotes = {}
        plan = {"makueni_coop": (800, 45, 4000), "machakos_agg": (600, 50, 2500), "muranga_farm": (300, 60, 1500)}
        for m in req.matches.select_related("supplier__user"):
            username = m.supplier.user.username
            if username not in plan:
                sourcing.respond_to_match(m.supplier.user, m, available=False, note="Stock committed elsewhere")
                self.info(f"{m.supplier.business_name} declined")
                continue
            qty, price, delivery = plan[username]
            sourcing.respond_to_match(m.supplier.user, m, available=True, quantity=qty)
            quotes[username] = sourcing.submit_quotation(
                m.supplier.user, req, m.inventory_item, qty, Decimal(price), today + timedelta(days=3),
                today + timedelta(days=5), delivery_cost=Decimal(delivery), includes_delivery=True)
            self.info(f"{quotes[username].reference} {m.supplier.business_name}: {qty} kg @ KES {price}/kg "
                      f"+ KES {delivery:,} delivery = KES {quotes[username].total:,}")

        sourcing.counter_quotation(buyer, quotes["machakos_agg"], price_per_unit=Decimal("47"),
                                   note="Can you do 47/kg for 600 kg?")
        sourcing.revise_quotation(U("machakos_agg"), quotes["machakos_agg"], accept_counter=True)
        sourcing.counter_quotation(buyer, quotes["muranga_farm"], quantity=Decimal("100"),
                                   note="We only need 100 kg more to complete our order.")
        sourcing.revise_quotation(U("muranga_farm"), quotes["muranga_farm"], accept_counter=True)
        self.info("Machakos agreed to KES 47/kg; Kandara Orchards agreed to supply 100 kg")

        comparison = sourcing.compare_offers(req)
        self.info("Offer comparison (landed cost per kg):")
        for row in comparison["offers"]:
            q = row["quotation"]
            self.info(f"   {q.supplier.business_name:<40} {q.quantity:>5g} kg  KES {row['landed_unit_cost']}/kg")
        self.info(f"Cheapest plan covers {comparison['cheapest_plan_quantity']:g} kg for "
                  f"KES {comparison['cheapest_plan_cost']:,} (budget KES {req.target_budget:,}: "
                  f"{'within' if comparison['within_budget'] else 'over'} budget)")
        if stop == "quotes":
            return self._done(req)

        # 8-9. Multi-supplier sourcing and confirmed supply agreement
        self.say("8-9", "Buyer accepts three offers (multi-supplier) and places one order; suppliers reconfirm")
        for q in quotes.values():
            q.refresh_from_db()
            sourcing.accept_quotation(buyer, q)
        order = orders.create_order(buyer, req, [q for q in quotes.values()])
        for line in order.lines.select_related("supplier__user"):
            orders.reconfirm_line(line.supplier.user, line, available=True, note="Stock reserved")
        order.refresh_from_db()
        self.info(f"Order {order.reference}: {order.lines.count()} suppliers, {order.total_quantity:g} kg, "
                  f"KES {order.total:,} — status: {order.get_status_display()}")
        from django.conf import settings

        from apps.payments.services import start_payment
        if settings.PAYMENT_PROVIDER == "simulated":
            payment = start_payment(buyer, order, buyer.phone or "0700000001")
            self.info(f"Buyer paid {payment.reference}: KES {payment.amount:,.0f} by M-Pesa (simulated, code "
                      f"{payment.receipt_number}); Origin 254 holds it until delivery is confirmed")
        else:
            self.info("Payment skipped: PAYMENT_PROVIDER is mpesa, so the buyer pays from the order page")
        if stop == "agreement":
            return self._done(req, order)

        # 10. Inspection and delivery tracking
        self.say(10, "QA inspection at source, then delivery tracking")
        for line in order.lines.all():
            orders.record_inspection(qa, line, Inspection.Stage.PRE_DISPATCH, Inspection.Result.PASSED,
                                     grade_observed=QualityGrade.GRADE_1,
                                     measurements={},
                                     accepted_quantity=line.quantity, notes="Ripe, firm fruit; meets jam-grade specification")
        self.info("All three lots passed pre-dispatch inspection")
        if stop == "inspection":
            return self._done(req, order)

        makueni_line = order.lines.get(supplier__user__username="makueni_coop")
        orders.assign_logistics(buyer, makueni_line.delivery, logi)
        self.info("Swift Agri Logistics assigned to the Makueni lot (refrigerated truck)")
        for line in order.lines.select_related("delivery", "supplier__user"):
            actor = logi if line.delivery.logistics_partner_id else line.supplier.user
            d = line.delivery
            if d.status == Delivery.Status.PENDING:
                orders.update_delivery(actor, d, Delivery.Status.SCHEDULED)
            orders.update_delivery(actor, d, Delivery.Status.PICKED_UP, vehicle_reg="KDA 123X",
                                   driver_name="Driver", note="Loaded at source")
            orders.update_delivery(actor, d, Delivery.Status.IN_TRANSIT, note="On Mombasa Road")
            orders.update_delivery(actor, d, Delivery.Status.DELIVERED, received_quantity=line.quantity,
                                   received_by="A. Otieno (stores)", note="Offloaded at factory")
            self.info(f"{line.supplier.business_name}: delivered {line.quantity:g} kg "
                      f"(handled by {actor.display_name})")
        if stop == "delivery":
            return self._done(req, order)

        # 11. Completion and feedback
        self.say(11, "Buyer confirms receipt; transaction completed and rated")
        for line in order.lines.select_related("supplier__user"):
            orders.complete_line(buyer, line, note="Received in good condition")
            create_review(buyer, line, rating=5 if line.supplier.user.username != "machakos_agg" else 4,
                          comment="Great fruit for jam.", quality_rating=5, timeliness_rating=5)
            create_review(line.supplier.user, line, rating=5, comment="Prompt, clear buyer.")
        order.refresh_from_db()
        req.refresh_from_db()
        self.info(f"Order {order.reference}: {order.get_status_display()}; request {req.reference}: "
                  f"{req.get_status_display()}")
        for line in order.lines.select_related("supplier"):
            s = line.supplier
            self.info(f"{s.business_name}: reliability {s.reliability_score}, rating {s.average_rating}, "
                      f"{s.completed_orders} completed")
        from apps.payments.models import Payout
        for po in Payout.objects.filter(order_line__order=order).select_related("supplier"):
            self.info(f"Payout {po.reference}: KES {po.amount:,.0f} to {po.supplier.business_name} "
                      f"({po.get_status_display().lower()})")
        self.info(f"Timeline: {order.events.count()} documented events")
        return self._done(req, order)

    def _done(self, req, order=None):
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Sign in at http://127.0.0.1:8000/app/ as mama_jam / Origin254!demo to see {req.reference}"
            + (f" and order {order.reference}." if order else ".")))
