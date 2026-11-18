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
        for role in (Role.FARMER, Role.PROCESSOR, Role.LOGISTICS, Role.ADMIN):
            self.assertEqual(User.objects.filter(role=role, email__endswith="@demo.origin254.example").count(), 10)
        demo_items = InventoryItem.objects.filter(quality_attributes__other_details__startswith="[Kenya demo]")
        self.assertEqual(demo_items.count(), 30)
        self.assertTrue(all(i.photos.count() >= 2 for i in demo_items))
        self.assertEqual(SourcingRequest.objects.count(), 18)
        self.assertGreater(Order.objects.count(), 0)
        self.assertTrue(User.objects.get(username="grace.wanjiku").check_password("test-pass-123"))

        call_command("seed_kenya", stdout=StringIO())  # second run adds nothing
        self.assertEqual(User.objects.filter(email__endswith="@demo.origin254.example").count(), 40)
        self.assertEqual(SourcingRequest.objects.count(), 18)
        self.assertEqual(demo_items.count(), 30)
