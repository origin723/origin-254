from django.test import Client, TestCase

from apps.catalog.models import Product
from apps.core.models import Location
from apps.sourcing.matching import nearest_first_plan, nearest_suppliers

from .factories import api, mango_request, seed, user


class NearestSupplierTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()
        cls.nairobi = Location.objects.get(name="Nairobi Industrial Area")
        cls.mango = Product.objects.get(name="Mango")

    def test_sorted_by_distance_and_only_verified_fresh_stock(self):
        results = nearest_suppliers(self.mango, self.nairobi)
        distances = [r.distance_km for r in results]
        self.assertEqual(distances, sorted(distances))
        names = [r.supplier.user.username for r in results]
        self.assertEqual(names[0], "machakos_agg")  # ~51 km, closest
        self.assertNotIn("kitui_farm", names)  # pending verification
        self.assertNotIn("meru_farm", names)  # stale stock
        self.assertGreater(results[0].road_km, results[0].distance_km)

    def test_filters(self):
        self.assertTrue(all(r.distance_km <= 100 for r in nearest_suppliers(self.mango, self.nairobi, radius_km=100)))
        big = nearest_suppliers(self.mango, self.nairobi, min_quantity=1000)
        self.assertTrue(big and all(r.item.quantity_available >= 1000 for r in big))

    def test_nearest_first_plan_covers_quantity(self):
        req, _ = mango_request()
        plan = nearest_first_plan(req, list(req.matches.all()))
        self.assertTrue(plan["fully_covered"])
        used = [float(m.distance_km) for m, _ in plan["plan"]]
        self.assertEqual(used, sorted(used))

    def test_web_page_and_request_sort(self):
        c = Client()
        c.force_login(user("mama_jam"))
        r = c.get("/app/suppliers/nearest/", {"product": "mangoes", "location": self.nairobi.pk})
        self.assertContains(r, "Machakos Fresh Produce Farm")
        self.assertContains(r, "Nearest")
        req, _ = mango_request()
        r = c.get(f"/app/requests/{req.pk}/?sort=nearest")
        self.assertContains(r, "Nearest suppliers suggestion")
        html = r.content.decode()
        self.assertLess(html.index("Machakos Fresh Produce"), html.index("Makueni Mango Growers"))

    def test_api(self):
        r = api("mama_jam").get("/api/v1/suppliers/nearest/", {"product": self.mango.pk, "location": self.nairobi.pk})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[0]["supplier"]["business_name"], "Machakos Fresh Produce Farm")
        self.assertEqual(api("mama_jam").get("/api/v1/suppliers/nearest/").status_code, 400)

    def test_sugar_fields_show_if_applicable_hint(self):
        c = Client()
        c.force_login(user("makueni_coop"))
        self.assertContains(c.get("/app/inventory/"), 'placeholder="if applicable"')
        c.force_login(user("mama_jam"))
        self.assertContains(c.get("/app/requests/new/"), 'placeholder="if applicable"')
