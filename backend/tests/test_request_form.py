from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.catalog.models import InventoryItem, Product, ProductVariety
from apps.core.models import Location
from apps.sourcing.models import SourcingRequest
from tests.factories import mango_request, quote, seed, user


class NewRequestPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def setUp(self):
        self.client.force_login(user("mama_jam"))
        self.today = timezone.localdate()

    def post(self, **over):
        data = {"product_name": "Mango", "variety_name": "", "quantity": "1500", "unit": "kg",
                "delivery_location": Location.objects.get(name="Nairobi Industrial Area").pk,
                "delivery_deadline": str(self.today + timedelta(days=7)), "min_quality_grade": "grade_1",
                "quality_notes": "", "intended_application": "Jam", "packaging_requirements": "",
                "target_budget": "", "delivery_arrangement": "supplier_delivers"}
        data.update(over)
        return self.client.post("/app/requests/new/", data, follow=True)

    def test_valid_request_reports_full_coverage(self):
        r = self.post()
        self.assertEqual(SourcingRequest.objects.count(), 1)
        self.assertContains(r, "full 1500 kg can be covered")

    def test_budget_must_be_positive(self):
        for value in ("-100", "0"):
            r = self.post(target_budget=value)
            self.assertContains(r, "The budget must be at least KES 1")
        self.assertEqual(SourcingRequest.objects.count(), 0)

    def test_crates_do_not_match_kg_stock(self):
        r = self.post(unit="crate", quantity="50")
        req = SourcingRequest.objects.get()
        self.assertEqual(req.matches.count(), 0)
        self.assertContains(r, "not comparable to crates")

    def test_tonnes_convert_to_kg(self):
        self.post(unit="tonne", quantity="1.5")
        self.assertGreater(SourcingRequest.objects.get().matches.count(), 0)

    def test_same_unit_crates_match(self):
        item = InventoryItem.objects.get(supplier__user__username="machakos_agg")
        item.unit, item.quantity_available = "crate", 40
        item.save()
        self.post(unit="crate", quantity="30")
        req = SourcingRequest.objects.get()
        self.assertEqual(list(req.matches.values_list("supplier__user__username", flat=True)), ["machakos_agg"])

    def test_no_match_explains_why(self):
        r = self.post(delivery_deadline=str(self.today))
        self.assertContains(r, "no supplier can meet it yet")
        self.assertContains(r, "cannot deliver before the deadline")
        r = self.post(product_name="Pineapple")
        self.assertContains(r, "No approved supplier currently lists Pineapple")

    def test_typed_names_match_the_catalogue(self):
        self.post(product_name="  mangoes ", variety_name="apple")
        req = SourcingRequest.objects.get()
        self.assertEqual(req.product, Product.objects.get(name="Mango"))
        self.assertEqual(req.variety, ProductVariety.objects.get(product__name="Mango", name="Apple"))
        self.assertGreater(req.matches.count(), 0)

    def test_any_new_product_can_be_requested(self):
        r = self.post(product_name="Baobab fruit", variety_name="Wild")
        req = SourcingRequest.objects.get()
        self.assertEqual(req.product.name, "Baobab fruit")
        self.assertEqual(req.product.category.name, "Other (supplier-added)")
        self.assertEqual(req.variety.name, "Wild")
        self.assertContains(r, "No approved supplier currently lists Baobab fruit")

    def test_new_product_then_matches_a_supplier_who_lists_it(self):
        self.post(product_name="Baobab fruit")
        req = SourcingRequest.objects.get()
        item = InventoryItem.objects.get(supplier__user__username="machakos_agg")
        item.product = Product.objects.get(name="Baobab fruit")
        item.variety = None
        item.save()
        from apps.sourcing.matching import run_matching
        run_matching(req)
        self.assertEqual(list(req.matches.values_list("supplier__user__username", flat=True)), ["machakos_agg"])

    def test_product_is_required(self):
        r = self.post(product_name=" ")
        self.assertContains(r, "This field is required.")
        self.assertEqual(SourcingRequest.objects.count(), 0)

    def test_no_product_dropdown(self):
        html = self.client.get("/app/requests/new/").content.decode()
        self.assertIn('name="product_name"', html)
        self.assertNotIn('<select name="product"', html)
        self.assertNotIn('<select name="variety"', html)

    def test_prefill_from_nearest_suppliers(self):
        r = self.client.get("/app/requests/new/", {"product": "Mango", "delivery_location": "5"})
        self.assertEqual(r.context["form"].initial["product_name"], "Mango")

    def test_api_accepts_typed_product(self):
        from tests.factories import api
        r = api("mama_jam").post("/api/v1/sourcing-requests/", {
            "product_name": "mangoes", "quantity": "500", "unit": "kg",
            "delivery_location_id": Location.objects.get(name="Nairobi Industrial Area").pk,
            "delivery_deadline": str(self.today + timedelta(days=7)), "intended_application": "Juice"},
            format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["product_name"], "Mango")
        r = api("mama_jam").post("/api/v1/sourcing-requests/", {
            "quantity": "500", "unit": "kg", "delivery_location_id": 1,
            "delivery_deadline": str(self.today + timedelta(days=7)), "intended_application": "Juice"},
            format="json")
        self.assertEqual(r.status_code, 400)

    def test_page_renders_iso_date_and_grouped_varieties(self):
        html = self.client.get("/app/requests/new/").content.decode()
        self.assertIn(f'value="{(self.today + timedelta(days=7)).isoformat()}"', html)
        self.assertIn(f'min="{self.today.isoformat()}"', html)

    def test_admin_is_redirected_with_explanation(self):
        self.client.force_login(user("admin"))
        r = self.client.get("/app/requests/new/", follow=True)
        self.assertEqual(r.redirect_chain[-1][0], "/app/admin/requests/")
        self.assertContains(r, "posted by buyer (processor) accounts")

    def test_suppliers_cannot_open_it(self):
        self.client.force_login(user("makueni_coop"))
        self.assertEqual(self.client.get("/app/requests/new/").status_code, 403)

    def test_quote_rejected_when_units_differ(self):
        req, _ = mango_request()
        item = InventoryItem.objects.get(supplier__user__username="makueni_coop")
        item.unit = "crate"
        item.save()
        from apps.core.exceptions import WorkflowError
        with self.assertRaises(WorkflowError):
            quote(req, "makueni_coop", "100", "45")
