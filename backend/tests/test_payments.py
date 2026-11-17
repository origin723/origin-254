import json
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.exceptions import WorkflowError
from apps.orders import services as orders
from apps.orders.models import Delivery, Inspection
from apps.payments import services as pay
from apps.payments.models import Payment, Payout
from apps.payments.providers import PushResult, normalise_phone
from apps.sourcing import services as sourcing

from .factories import api, mango_request, quote, seed, user

TOKEN = "test-callback-token-123"


def confirmed_order():
    req, _ = mango_request()
    q1 = quote(req, "makueni_coop", "800", "45", "4000")
    q2 = quote(req, "machakos_agg", "600", "50", "2500")
    for q in (q1, q2):
        sourcing.accept_quotation(user("mama_jam"), q)
    order = orders.create_order(user("mama_jam"), req, [q1, q2])
    for line in order.lines.select_related("supplier__user"):
        orders.reconfirm_line(line.supplier.user, line, True)
    order.refresh_from_db()
    return order


def deliver_and_complete(order, line):
    qa = user("qa_officer")
    orders.record_inspection(qa, line, Inspection.Stage.PRE_DISPATCH, Inspection.Result.PASSED,
                             accepted_quantity=line.quantity)
    d = line.delivery
    actor = line.supplier.user
    if d.status == Delivery.Status.PENDING:
        orders.update_delivery(actor, d, Delivery.Status.SCHEDULED)
    orders.update_delivery(actor, d, Delivery.Status.PICKED_UP)
    orders.update_delivery(actor, d, Delivery.Status.DELIVERED, received_quantity=line.quantity, received_by="Stores")
    orders.complete_line(user("mama_jam"), line)


class PhoneTests(TestCase):
    def test_formats(self):
        for raw in ("0712345678", "+254712345678", "254712345678", "712345678", "0112 345 678"):
            self.assertTrue(normalise_phone(raw).startswith("254"), raw)
        for bad in ("12345", "0812345678", "", "+1 555 1234"):
            with self.assertRaises(ValueError):
                normalise_phone(bad)


@override_settings(PAYMENT_PROVIDER="simulated", PAYOUT_PROVIDER="manual", PLATFORM_FEE_PERCENT="2")
class SimulatedPaymentFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def setUp(self):
        self.order = confirmed_order()

    def test_buyer_pays_and_suppliers_paid_after_delivery(self):
        self.assertEqual(pay.amount_due(self.order), Decimal("72500"))
        p = pay.start_payment(user("mama_jam"), self.order, "0712345678")
        self.assertEqual(p.status, Payment.Status.SUCCEEDED)
        self.assertTrue(p.receipt_number)
        self.assertEqual(p.phone, "254712345678")
        self.assertTrue(pay.is_paid(self.order))
        self.assertEqual(pay.amount_due(self.order), 0)
        with self.assertRaises(WorkflowError):
            pay.start_payment(user("mama_jam"), self.order, "0712345678")

        line = self.order.lines.get(supplier__user__username="makueni_coop")
        deliver_and_complete(self.order, line)
        payout = Payout.objects.get(order_line=line)
        self.assertEqual(payout.status, Payout.Status.READY)
        self.assertEqual(payout.gross_amount, Decimal("40000.00"))
        self.assertEqual(payout.fee, Decimal("800.00"))
        self.assertEqual(payout.amount, Decimal("39200.00"))
        pay.record_manual_payout(user("admin"), payout, "QK12ABC345")
        payout.refresh_from_db()
        self.assertEqual(payout.status, Payout.Status.PAID)
        self.assertFalse(Payout.objects.filter(order_line__supplier__user__username="machakos_agg").exists())

    def test_cannot_pay_before_reconfirmation_or_for_someone_else(self):
        with self.assertRaises(WorkflowError):
            pay.start_payment(user("new_juice_co"), self.order, "0712345678")
        self.order.status = "pending_confirmation"
        with self.assertRaises(WorkflowError):
            pay.start_payment(user("mama_jam"), self.order, "0712345678")

    def test_invalid_phone(self):
        with self.assertRaises(WorkflowError):
            pay.start_payment(user("mama_jam"), self.order, "12345")
        self.assertFalse(Payment.objects.exists())

    def test_completing_before_payment_releases_payout_once_paid(self):
        line = self.order.lines.get(supplier__user__username="machakos_agg")
        deliver_and_complete(self.order, line)
        self.assertFalse(Payout.objects.exists())
        pay.start_payment(user("mama_jam"), self.order, "0712345678")
        self.assertEqual(Payout.objects.get().order_line, line)

    @override_settings(PAYOUT_PROVIDER="simulated")
    def test_automatic_payout(self):
        pay.start_payment(user("mama_jam"), self.order, "0712345678")
        line = self.order.lines.get(supplier__user__username="machakos_agg")
        with self.captureOnCommitCallbacks(execute=True):
            deliver_and_complete(self.order, line)
        payout = Payout.objects.get(order_line=line)
        self.assertEqual(payout.status, Payout.Status.PAID)
        self.assertEqual(payout.method, Payout.Method.SIMULATED)

    def test_dispute_holds_payout(self):
        pay.start_payment(user("mama_jam"), self.order, "0712345678")
        line = self.order.lines.get(supplier__user__username="machakos_agg")
        orders.raise_dispute(user("mama_jam"), self.order, "Late", line)
        deliver_and_complete(self.order, line)
        payout = Payout.objects.get()
        self.assertEqual(payout.status, Payout.Status.ON_HOLD)
        with self.assertRaises(WorkflowError):
            pay.record_manual_payout(user("admin"), payout, "QK12ABC345")
        pay.release_hold(user("admin"), payout)
        payout.refresh_from_db()
        self.assertEqual(payout.status, Payout.Status.READY)

    def test_refund_due_after_cancelled_lot(self):
        pay.start_payment(user("mama_jam"), self.order, "0712345678")
        line = self.order.lines.get(supplier__user__username="machakos_agg")
        orders.record_inspection(user("qa_officer"), line, Inspection.Stage.PRE_DISPATCH, Inspection.Result.FAILED,
                                 notes="Bruised")
        self.order.refresh_from_db()
        self.assertEqual(pay.refund_due(self.order), Decimal("32500.00"))
        pay.mark_refunded(user("admin"), self.order, "RFD123456", "32500")
        self.assertEqual(pay.refund_due(self.order), 0)
        self.assertTrue(pay.is_paid(self.order))

    @override_settings(REQUIRE_PAYMENT_BEFORE_DISPATCH=True)
    def test_dispatch_can_require_payment(self):
        line = self.order.lines.get(supplier__user__username="machakos_agg")
        orders.record_inspection(user("qa_officer"), line, Inspection.Stage.PRE_DISPATCH, Inspection.Result.PASSED,
                                 accepted_quantity=line.quantity)
        orders.update_delivery(line.supplier.user, line.delivery, Delivery.Status.SCHEDULED)
        with self.assertRaises(WorkflowError):
            orders.update_delivery(line.supplier.user, line.delivery, Delivery.Status.PICKED_UP)
        pay.start_payment(user("mama_jam"), self.order, "0712345678")
        orders.update_delivery(line.supplier.user, line.delivery, Delivery.Status.PICKED_UP)

    def test_order_page_shows_pay_button_and_pays(self):
        self.client.force_login(user("mama_jam"))
        r = self.client.get(f"/app/orders/{self.order.pk}/")
        self.assertContains(r, "Pay KES 72500 with M-Pesa")
        r = self.client.post(f"/app/orders/{self.order.pk}/pay/", {"phone": "0712345678"}, follow=True)
        self.assertContains(r, "Paid KES 72,500")
        self.assertContains(r, "badge ok\">Paid</span>")
        self.client.force_login(user("makueni_coop"))
        self.assertNotContains(self.client.get(f"/app/orders/{self.order.pk}/"), "with M-Pesa</button>")

    def test_admin_payments_page(self):
        pay.start_payment(user("mama_jam"), self.order, "0712345678")
        deliver_and_complete(self.order, self.order.lines.get(supplier__user__username="makueni_coop"))
        self.client.force_login(user("admin"))
        r = self.client.get("/app/admin/payments/")
        self.assertContains(r, "Makueni Mango Growers")
        payout = Payout.objects.get()
        r = self.client.post("/app/admin/payments/", {"payout": payout.pk, "action": "record",
                                                      "receipt_number": "qk99zzz111"}, follow=True)
        self.assertContains(r, "Payout recorded as paid.")
        payout.refresh_from_db()
        self.assertEqual(payout.receipt_number, "QK99ZZZ111")
        self.client.force_login(user("mama_jam"))
        self.assertEqual(self.client.get("/app/admin/payments/").status_code, 403)

    def test_api(self):
        r = api("mama_jam").post("/api/v1/payments/", {"order": self.order.pk, "phone": "0712345678"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "succeeded")
        self.assertEqual(api("mama_jam").get("/api/v1/payments/").data["count"], 1)
        self.assertEqual(api("new_juice_co").get("/api/v1/payments/").data["count"], 0)
        r = api("makueni_coop").post("/api/v1/payments/", {"order": self.order.pk, "phone": "0712345678"},
                                     format="json")
        self.assertEqual(r.status_code, 403)


class FakeMpesa:
    name = "mpesa"

    def stk_push(self, payment):
        return PushResult(checkout_request_id="ws_CO_TEST_1", merchant_request_id="m-1", message="Sent")


@override_settings(PAYMENT_PROVIDER="mpesa", MPESA_CALLBACK_TOKEN=TOKEN, PAYOUT_PROVIDER="manual")
class MpesaCallbackTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def setUp(self):
        self.order = confirmed_order()
        with mock.patch("apps.payments.services.get_provider", return_value=FakeMpesa()):
            self.payment = pay.start_payment(user("mama_jam"), self.order, "0712345678")

    def callback(self, code=0, amount=72500, receipt="QKA1B2C3D4", token=TOKEN):
        body = {"Body": {"stkCallback": {"MerchantRequestID": "m-1", "CheckoutRequestID": "ws_CO_TEST_1",
                                         "ResultCode": code, "ResultDesc": "ok" if code == 0 else "Cancelled"}}}
        if code == 0:
            body["Body"]["stkCallback"]["CallbackMetadata"] = {"Item": [
                {"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "PhoneNumber", "Value": 254712345678}]}
        return self.client.post(f"/payments/mpesa/stk/{token}/", json.dumps(body), content_type="application/json")

    def test_pending_until_callback(self):
        self.assertEqual(self.payment.status, Payment.Status.PENDING)
        self.assertEqual(pay.payment_state(self.order), "pending")
        with self.assertRaises(WorkflowError):  # no double prompt
            with mock.patch("apps.payments.services.get_provider", return_value=FakeMpesa()):
                pay.start_payment(user("mama_jam"), self.order, "0712345678")

    def test_success_callback(self):
        r = self.callback()
        self.assertEqual(r.json()["ResultCode"], 0)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(self.payment.receipt_number, "QKA1B2C3D4")
        self.callback(code=1032)  # late duplicate is ignored
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.SUCCEEDED)

    def test_cancelled_by_user(self):
        self.callback(code=1032)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.CANCELLED)
        self.assertEqual(pay.payment_state(self.order), "unpaid")

    def test_amount_mismatch_rejected(self):
        self.callback(amount=10)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.FAILED)

    def test_wrong_token_is_404(self):
        self.assertEqual(self.callback(token="nope").status_code, 404)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)

    def test_missing_config_gives_clear_error(self):
        order = self.order
        Payment.objects.all().delete()
        with self.assertRaises(WorkflowError) as ctx:
            pay.start_payment(user("mama_jam"), order, "0712345678")
        self.assertIn("M-Pesa is not configured", ctx.exception.message)


class CommissionTests(TestCase):
    """Origin 254 keeps 10% of every sale; the seller receives the rest."""

    @classmethod
    def setUpTestData(cls):
        seed()

    def test_default_is_ten_percent(self):
        fee, net = pay.split(Decimal("40000"))
        self.assertEqual((fee, net), (Decimal("4000.00"), Decimal("36000.00")))

    @override_settings(PAYMENT_PROVIDER="simulated", PAYOUT_PROVIDER="manual")
    def test_seller_receives_ninety_percent_and_admin_sees_commission(self):
        order = confirmed_order()
        pay.start_payment(user("mama_jam"), order, "0712345678")
        line = order.lines.get(supplier__user__username="makueni_coop")
        deliver_and_complete(order, line)
        payout = Payout.objects.get(order_line=line)
        self.assertEqual(payout.gross_amount, Decimal("40000.00"))
        self.assertEqual(payout.fee, Decimal("4000.00"))
        self.assertEqual(payout.amount, Decimal("36000.00"))
        summary = pay.commission_summary()
        self.assertEqual(summary["pending"], Decimal("4000.00"))
        pay.record_manual_payout(user("admin"), payout, "QK12ABC345")
        summary = pay.commission_summary()
        self.assertEqual(summary["earned"], Decimal("4000.00"))
        self.assertEqual(summary["paid_out"], Decimal("36000.00"))
        self.assertEqual(summary["held"], Decimal("72500") - Decimal("40000"))

        # Seller's payments page
        self.client.force_login(user("makueni_coop"))
        r = self.client.get("/app/payments/")
        self.assertContains(r, "KES 36,000")
        self.assertContains(r, "− KES 4,000")
        self.assertContains(r, "10% commission")
        # Admin pages
        self.client.force_login(user("admin"))
        self.assertContains(self.client.get("/app/admin/"), "Commission earned (10%)")
        self.assertContains(self.client.get("/app/admin/payments/"), "KES 4,000")
        self.assertRedirects(self.client.get("/app/payments/"), "/app/admin/payments/")

    @override_settings(PAYMENT_PROVIDER="simulated")
    def test_payment_buttons_on_dashboards(self):
        order = confirmed_order()
        self.client.force_login(user("mama_jam"))
        r = self.client.get("/app/")
        self.assertContains(r, 'href="/app/payments/"')
        self.assertContains(r, "1 to pay")
        r = self.client.get("/app/payments/")
        self.assertContains(r, "Pay KES 72,500")
        r = self.client.post(f"/app/orders/{order.pk}/pay/", {"phone": "0712345678", "next": "payments"})
        self.assertRedirects(r, "/app/payments/")
        self.assertTrue(pay.is_paid(order))
        self.client.force_login(user("makueni_coop"))
        r = self.client.get("/app/")
        self.assertContains(r, "My payments")
        r = self.client.get("/app/payments/")
        self.assertContains(r, "KES 36,000")  # upcoming: 40,000 - 10%
        self.assertContains(r, "Yes, held by Origin 254")
        self.client.force_login(user("admin"))
        self.assertContains(self.client.get("/app/admin/"), "Payments &amp; commission")
        self.client.force_login(user("swift_logistics"))
        self.assertEqual(self.client.get("/app/payments/").status_code, 403)


@override_settings(PAYMENT_PROVIDER="simulated")
class BuyerExpensesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def test_total_expenses_and_main_pay_button(self):
        order = confirmed_order()  # 800 kg @ 45 + 4,000 delivery; 600 kg @ 50 + 2,500 delivery
        self.client.force_login(user("mama_jam"))
        r = self.client.get("/app/payments/")
        e = r.context["expenses"]
        self.assertEqual(e["produce"], Decimal("66000.00"))
        self.assertEqual(e["delivery"], Decimal("6500.00"))
        self.assertEqual(e["total"], Decimal("72500.00"))
        self.assertContains(r, "Total expenses")
        self.assertContains(r, "Pay KES 72,500 with M-Pesa")
        r = self.client.post("/app/payments/pay/", {"order": order.pk, "phone": "0712345678"}, follow=True)
        self.assertContains(r, "Paid KES 72,500")
        self.assertContains(r, "All paid")
        self.assertTrue(pay.is_paid(order))

    def test_cannot_pay_someone_elses_order(self):
        order = confirmed_order()
        self.client.force_login(user("new_juice_co"))
        r = self.client.post("/app/payments/pay/", {"order": order.pk, "phone": "0712345678"}, follow=True)
        self.assertContains(r, "Choose an order to pay.")
        self.assertFalse(Payment.objects.exists())
