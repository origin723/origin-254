from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.models import BuyerProfile, LogisticsProfile, SupplierProfile, VerificationStatus
from apps.catalog.models import InventoryItem
from apps.orders.models import Delivery, OrderLine
from apps.sourcing.models import Quotation

APPROVED = VerificationStatus.APPROVED


class SeedActivityTests(TestCase):
    def test_every_verified_account_gets_activity_and_rerun_adds_nothing(self):
        call_command("seed_kenya", password="x", stdout=StringIO())
        out = StringIO()
        call_command("seed_activity", stdout=out)
        self.assertIn("skipped 0", out.getvalue())
        for farmer in SupplierProfile.objects.filter(verification_status=APPROVED):
            self.assertGreaterEqual(InventoryItem.objects.filter(supplier=farmer, is_active=True).count(), 5)
            self.assertTrue(OrderLine.objects.filter(supplier=farmer, status=OrderLine.Status.COMPLETED).exists())
            self.assertTrue(Quotation.objects.filter(supplier=farmer, status=Quotation.Status.SUBMITTED).exists())
            self.assertTrue(Delivery.objects.filter(order_line__supplier=farmer, logistics_partner__isnull=True)
                            .exists(), farmer.business_name)
        for buyer in BuyerProfile.objects.filter(verification_status=APPROVED):
            self.assertTrue(OrderLine.objects.filter(order__buyer=buyer.user, status=OrderLine.Status.COMPLETED)
                            .exists(), buyer.business_name)
        for lp in LogisticsProfile.objects.filter(verification_status=APPROVED):
            self.assertTrue(Delivery.objects.filter(logistics_partner=lp.user).exists(), lp.business_name)

        lines = OrderLine.objects.count()
        rerun = StringIO()
        call_command("seed_activity", stdout=rerun)
        self.assertIn("listings added 0", rerun.getvalue())
        self.assertEqual(OrderLine.objects.count(), lines)
