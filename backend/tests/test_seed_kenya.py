from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.models import Role, User
from apps.catalog.models import InventoryItem
from apps.orders.models import Order
from apps.sourcing.models import SourcingRequest


class SeedKenyaTests(TestCase):
    def test_populates_every_role_and_workflow_and_is_idempotent(self):
        out = StringIO()
        call_command("seed_kenya", password="test-pass-123", stdout=out)
        counts = {Role.FARMER: 13, Role.PROCESSOR: 10, Role.LOGISTICS: 10, Role.ADMIN: 10}  # 10 + 3 beef ranchers
        for role, n in counts.items():
            self.assertEqual(User.objects.filter(role=role, email__endswith="@demo.origin254.example").count(), n)
        demo_items = InventoryItem.objects.filter(quality_attributes__other_details__startswith="[Kenya demo]")
        self.assertEqual(demo_items.count(), 36)
        self.assertTrue(all(i.photos.count() >= 2 for i in demo_items))
        self.assertEqual(SourcingRequest.objects.count(), 19)
        beef = demo_items.filter(product__name="Beef")
        self.assertEqual(beef.count(), 3)
        self.assertTrue(SourcingRequest.objects.get(product__name="Beef").matches.exists())
        self.assertGreater(Order.objects.count(), 0)
        self.assertTrue(User.objects.get(username="grace.wanjiku").check_password("test-pass-123"))

        call_command("seed_kenya", stdout=StringIO())  # second run adds nothing
        self.assertEqual(User.objects.filter(email__endswith="@demo.origin254.example").count(), 43)
        self.assertEqual(SourcingRequest.objects.count(), 19)
        self.assertEqual(demo_items.count(), 36)
