from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.catalog.models import InventoryItem
from apps.orders.models import OrderLine, SaleRecord
from apps.orders.services import record_sale


def user(name):
    return User.objects.get(username=name)


class SaleHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("mango_demo", stdout=StringIO())

    def test_completed_purchases_are_saved(self):
        self.assertEqual(SaleRecord.objects.count(), 3)
        rec = SaleRecord.objects.get(supplier__user__username="machakos_agg")
        self.assertEqual(rec.product_name, "Mango")
        self.assertEqual(rec.variety_name, "Ngowe")
        self.assertEqual(rec.quantity, 600)
        self.assertEqual(rec.price_per_unit, 47)
        self.assertEqual(rec.total, 600 * 47 + 2500)
        self.assertEqual(rec.buyer_name, "Mama Jam Foods Ltd")
        self.assertEqual(sum(r.total for r in SaleRecord.objects.all()), 78200)

    def test_recording_twice_does_not_duplicate(self):
        line = OrderLine.objects.filter(status="completed").first()
        record_sale(line)
        self.assertEqual(SaleRecord.objects.count(), 3)

    def test_history_survives_listing_changes(self):
        item = InventoryItem.objects.get(supplier__user__username="makueni_coop")
        item.quantity_available = 0
        item.is_active = False
        item.save()
        rec = SaleRecord.objects.get(inventory_item=item)
        self.assertEqual(rec.quantity, 800)
        self.client.force_login(user("mama_jam"))
        self.assertContains(self.client.get("/app/history/"), "Makueni Mango Growers")

    def test_buyer_sees_all_purchases(self):
        self.client.force_login(user("mama_jam"))
        r = self.client.get("/app/history/")
        self.assertContains(r, "Purchase history")
        self.assertEqual(len(r.context["records"]), 3)
        self.assertContains(r, "KES 78,200")

    def test_supplier_sees_only_own_sales(self):
        self.client.force_login(user("muranga_farm"))
        r = self.client.get("/app/history/")
        self.assertContains(r, "Sales history")
        self.assertEqual([x.supplier_name for x in r.context["records"]], ["Kandara Orchards (J. Wanjiru)"])
        self.assertNotContains(r, "Makueni Mango Growers")

    def test_other_users_see_nothing(self):
        for name in ("embu_farm", "new_juice_co", "swift_logistics"):
            self.client.force_login(user(name))
            self.assertEqual(len(self.client.get("/app/history/").context["records"]), 0, name)

    def test_admin_sees_everything_and_search(self):
        self.client.force_login(user("admin"))
        self.assertEqual(len(self.client.get("/app/history/").context["records"]), 3)
        self.assertEqual(len(self.client.get("/app/history/?q=ngowe").context["records"]), 1)

    def test_api(self):
        c = APIClient()
        c.force_authenticate(user("makueni_coop"))
        data = c.get("/api/v1/history/").data
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["product_name"], "Mango")
        c.force_authenticate(user("mama_jam"))
        self.assertEqual(c.get("/api/v1/history/").data["count"], 3)
