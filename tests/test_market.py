from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import InventoryItem, QualityGrade
from apps.comms.models import Notification
from apps.core.models import Location
from apps.orders.models import SaleRecord
from apps.sourcing.market import market_report
from apps.sourcing.matching import offer_new_stock
from apps.sourcing.models import Match

from .factories import mango_request, seed, user


def png(i):
    return SimpleUploadedFile(f"p{i}.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "image/png")


class MarketSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def setUp(self):
        self.req, _ = mango_request(target_budget=Decimal("75000"))

    def get(self, username, product):
        self.client.force_login(user(username))
        return self.client.get("/app/market/", {"product": product})

    def test_dashboard_has_market_search(self):
        self.client.force_login(user("embu_farm"))
        r = self.client.get("/app/")
        self.assertContains(r, "Is there a market for my product?")
        self.assertContains(r, 'action="/app/market/"')

    def test_shows_open_demand_for_typed_product(self):
        r = self.get("embu_farm", "mangoes")
        self.assertContains(r, "Mango")
        report = r.context["report"]
        self.assertEqual(len(report.rows), 1)
        self.assertEqual(report.demand_kg, 1500)
        self.assertEqual(report.buyers, 1)
        row = report.rows[0]
        self.assertEqual(row.budget_per_unit, Decimal("50.00"))
        self.assertIsNotNone(row.distance_km)
        self.assertIsNone(row.my_match)  # embu's stock is grade 2, below the request
        self.assertContains(r, "Some demand")
        self.assertNotContains(r, "Mama Jam")  # buyer identity hidden until matched

    def test_matched_supplier_gets_link_to_opportunity(self):
        r = self.get("makueni_coop", "Mango")
        row = r.context["report"].rows[0]
        self.assertIsNotNone(row.my_match)
        self.assertContains(r, f"/app/opportunities/{row.my_match.pk}/")

    def test_unknown_product(self):
        r = self.get("embu_farm", "Baobab")
        self.assertContains(r, "No market for “Baobab” on Origin 254 yet")
        self.assertContains(r, "/app/inventory/?product=Baobab")

    def test_no_open_requests_but_recent_sales(self):
        self.req.status = "cancelled"
        self.req.save()
        SaleRecord.objects.create(order_reference="PO-X", buyer_name="B", supplier_name="S", product_name="Mango",
                                  quality_grade="grade_1", quantity=100, unit="kg", price_per_unit=Decimal("44"),
                                  total=Decimal("4400"), completed_at=timezone.now())
        SaleRecord.objects.create(order_reference="PO-Y", buyer_name="B", supplier_name="S", product_name="Mango",
                                  quality_grade="grade_1", quantity=1, unit="tonne", price_per_unit=Decimal("50000"),
                                  total=Decimal("50000"), completed_at=timezone.now())
        report = market_report("mango", user("embu_farm").supplier_profile)
        self.assertEqual(report.rows, [])
        self.assertEqual(report.level, "past")
        self.assertEqual(report.avg_price_kg, Decimal("47.00"))
        self.assertEqual(report.sales_kg, 1100)

    def test_past_deadline_and_own_requests_excluded(self):
        self.req.delivery_deadline = timezone.localdate() - timedelta(days=1)
        self.req.save()
        self.assertEqual(market_report("Mango", user("embu_farm").supplier_profile).rows, [])

    def test_only_suppliers_can_use_it(self):
        self.client.force_login(user("mama_jam"))
        self.assertEqual(self.client.get("/app/market/", {"product": "Mango"}).status_code, 403)

    def test_short_query_shows_nothing(self):
        self.assertIsNone(self.get("embu_farm", "m").context["report"])


class NewStockIsOfferedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def test_new_listing_is_added_to_waiting_request_without_disturbing_others(self):
        req, _ = mango_request()
        before = {m.pk: (m.rank, m.status, m.suggested_quantity) for m in req.matches.all()}
        self.client.force_login(user("embu_farm"))
        today = timezone.localdate()
        r = self.client.post("/app/inventory/", {
            "product_name": "Mango", "variety_name": "Apple", "quantity_available": "500", "unit": "kg",
            "quality_grade": "grade_1", "price_per_unit": "40", "location": Location.objects.get(name="Embu Town").pk,
            "available_from": str(today), "available_until": str(today + timedelta(days=14)),
            "can_deliver": "on", "packaging": "Crates", "photos": [png(1), png(2)]}, follow=True)
        self.assertContains(r, "1 buyer request for Mango matched your stock")
        new = Match.objects.get(sourcing_request=req, supplier__user__username="embu_farm",
                                inventory_item__quality_grade=QualityGrade.GRADE_1)
        self.assertEqual(new.rank, max(r for r, _, _ in before.values()) + 1)
        self.assertEqual({m.pk: (m.rank, m.status, m.suggested_quantity) for m in req.matches.exclude(pk=new.pk)},
                         before)
        self.assertTrue(Notification.objects.filter(user=user("mama_jam"),
                                                    title__contains="New supplier").exists())

    def test_offer_is_idempotent_and_respects_rules(self):
        req, _ = mango_request()
        embu = InventoryItem.objects.get(supplier__user__username="embu_farm")
        self.assertEqual(offer_new_stock(embu), [])  # grade 2: not good enough
        embu.quality_grade = QualityGrade.GRADE_1
        embu.save()
        self.assertEqual(len(offer_new_stock(embu)), 1)
        self.assertEqual(offer_new_stock(embu), [])  # already matched

    def test_suggested_quantity_fills_only_the_gap(self):
        req, _ = mango_request(quantity=Decimal("2000"))  # demo stock covers 1,700 kg
        embu = InventoryItem.objects.get(supplier__user__username="embu_farm")
        embu.quality_grade = QualityGrade.GRADE_1
        embu.save()
        match = offer_new_stock(embu)[0]
        planned = sum(m.suggested_quantity for m in req.matches.exclude(pk=match.pk))
        self.assertEqual(match.suggested_quantity, min(Decimal("2000") - planned, Decimal("400")))
