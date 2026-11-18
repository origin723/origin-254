from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase

from apps.accounts.models import User
from apps.catalog.models import Category


class MarketSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_kenya", password="test-pass-123", stdout=StringIO())

    def client_for(self, username):
        c = Client()
        c.force_login(User.objects.get(username=username))
        return c

    def test_farmer_sees_demand_by_product_and_processors(self):
        c = self.client_for("grace.wanjiku")
        html = c.get("/app/market/").content.decode()
        self.assertIn("Products buyers need", html)
        self.assertIn("Market details", html)
        self.assertIn("Beans", html)  # two processors have open beans requests
        self.assertNotIn("Rift Valley Unga Mills", html)  # buyer names are not tied to demand rows
        people = c.get("/app/market/?tab=people").content.decode()
        self.assertIn("Nyota Juice Ltd", people)
        self.assertIn("Mango", people)  # what that processor buys
        self.assertNotIn("Kerio Valley Honey", people)  # pending verification: not listed

    def test_processor_sees_stock_with_photos_and_farmers(self):
        c = self.client_for("wambui.njoroge")
        html = c.get("/app/market/").content.decode()
        self.assertIn("Products in stock", html)
        self.assertIn("Wanjiku Orchards", html)
        self.assertIn("/app/photos/", html)  # listing thumbnails
        self.assertIn("/app/requests/new/?product=", html)
        farmers = c.get("/app/market/?tab=people").content.decode()
        self.assertIn("Sankale Ranch", farmers)
        self.assertNotIn("Barasa Western Granary", farmers)  # pending verification

    def test_category_county_and_text_filters(self):
        c = self.client_for("wambui.njoroge")
        meat = Category.objects.get(name="Meat & livestock")
        html = c.get(f"/app/market/?category={meat.pk}").content.decode()
        self.assertIn("Kajiado Plains Beef", html)
        self.assertNotIn("Wanjiku Orchards", html)
        html = c.get("/app/market/?county=Nyandarua").content.decode()
        self.assertIn("Njeri Ol Kalou Growers", html)
        self.assertNotIn("Sankale Ranch", html)
        html = c.get("/app/market/?tab=people&q=avocado").content.decode()
        self.assertIn("Wanjiku Orchards", html)
        self.assertNotIn("Kajiado Plains Beef", html)
        combined = c.get(f"/app/market/?category={meat.pk}&county=Narok").content.decode()
        self.assertIn("Sankale Ranch", combined)
        self.assertNotIn("Kajiado Plains Beef", combined)

    def test_farmer_product_detail_still_works(self):
        c = self.client_for("grace.wanjiku")
        r = c.get("/app/market/?product=Mango")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Buyers looking for mango")

    def test_logistics_cannot_use_market_search(self):
        self.assertEqual(self.client_for("john.kamau").get("/app/market/").status_code, 403)


class ProductPickerTests(TestCase):
    """Product fields accept free typing and offer the known products to pick from (<datalist>)."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_kenya", password="test-pass-123", stdout=StringIO())

    def test_product_fields_link_to_product_list(self):
        for username, pages in [("wambui.njoroge", ["/app/suppliers/nearest/", "/app/requests/new/", "/app/market/"]),
                                ("grace.wanjiku", ["/app/inventory/", "/app/market/"])]:
            c = Client()
            c.force_login(User.objects.get(username=username))
            for page in pages:
                html = c.get(page).content.decode()
                self.assertIn('list="product-options"', html, page)
                self.assertIn('<datalist id="product-options">', html, page)
                self.assertIn('<option value="Sukuma wiki">', html, page)

    def test_typed_product_not_in_list_still_accepted(self):
        c = Client()
        c.force_login(User.objects.get(username="wambui.njoroge"))
        loc = User.objects.get(username="wambui.njoroge").buyer_profile.location_id
        r = c.get("/app/suppliers/nearest/", {"product": "Dragon fruit", "location": loc})
        self.assertEqual(r.status_code, 200)
