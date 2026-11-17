from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from apps.catalog.models import InventoryItem, Product, ProductVariety
from apps.core.models import AuditLog, Location

from .factories import mango_request, seed, user


class TypedProductInventoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def post(self, product_name, variety_name="", qty="500", n_photos=2):
        c = Client()
        c.force_login(user("embu_farm"))
        today = timezone.localdate()
        return c.post("/app/inventory/", {
            "product_name": product_name, "variety_name": variety_name, "quantity_available": qty, "unit": "kg",
            "quality_grade": "grade_1", "price_per_unit": "40", "location": Location.objects.get(name="Embu Town").pk,
            "available_from": str(today), "available_until": str(today + timedelta(days=14)),
            "can_deliver": "on", "packaging": "Crates", "other_details": "Hand-picked",
            "photos": [SimpleUploadedFile(f"p{i}.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "image/png")
                       for i in range(n_photos)]}, follow=True)

    def test_new_listing_requires_two_photos(self):
        before = InventoryItem.objects.count()
        r = self.post("Mango", "Kent", n_photos=1)
        self.assertContains(r, "at least 2 photos")
        self.assertEqual(InventoryItem.objects.count(), before)
        self.post("Mango", "Kent", n_photos=2)
        item = InventoryItem.objects.latest("created_at")
        self.assertEqual(item.photos.count(), 2)
        self.assertTrue(item.has_required_photos)

    def test_form_has_text_inputs_not_dropdowns(self):
        c = Client()
        c.force_login(user("embu_farm"))
        html = c.get("/app/inventory/").content.decode()
        self.assertIn('name="product_name"', html)
        self.assertIn('name="variety_name"', html)
        self.assertNotIn('<select name="product"', html)
        self.assertNotIn('<select name="variety"', html)

    def test_typed_name_matches_existing_product_and_variety(self):
        self.post("  mangoes ", "apple")
        item = InventoryItem.objects.filter(supplier__user__username="embu_farm").latest("created_at")
        self.assertEqual(item.product.name, "Mango")
        self.assertEqual(item.variety.name, "Apple")
        self.assertEqual(Product.objects.filter(name__iexact="mangoes").count(), 0)

    def test_typed_stock_is_matched_to_buyer_requests(self):
        self.post("MANGO", "apple", qty="2000")
        req, _ = mango_request()
        self.assertTrue(req.matches.filter(supplier__user__username="embu_farm").exists())

    def test_new_product_and_variety_are_created_for_review(self):
        r = self.post("Macadamia", "Murang'a 20")
        self.assertContains(r, "new to Origin 254")
        product = Product.objects.get(name="Macadamia")
        self.assertEqual(product.category.name, "Other (supplier-added)")
        self.assertTrue(ProductVariety.objects.filter(product=product, name="Murang'a 20").exists())
        self.assertTrue(AuditLog.objects.filter(action="catalog.product_added_by_supplier").exists())

    def test_variety_is_optional_and_product_required(self):
        self.post("Avocado")
        item = InventoryItem.objects.filter(supplier__user__username="embu_farm").latest("created_at")
        self.assertIsNone(item.variety)
        r = self.post("")
        self.assertContains(r, "This field is required")
