"""End-to-end procurement workflow through the REST API, plus business rules."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.catalog.models import InventoryItem, Product
from apps.core.models import AuditLog, Location
from apps.orders.models import Delivery, Order, OrderLine
from apps.sourcing.models import Match, SourcingRequest

from .factories import api, mango_request, quote, seed, user


class MangoScenarioAPITest(TestCase):
    """The MVP demonstration, driven entirely over HTTP as each role."""

    @classmethod
    def setUpTestData(cls):
        seed()

    def test_full_mango_jam_scenario(self):
        today = timezone.localdate()
        buyer = api("mama_jam")

        # 1. Register the request
        r = buyer.post("/api/v1/sourcing-requests/", {
            "product": Product.objects.get(name="Mango").pk, "quantity": "1500", "unit": "kg",
            "delivery_location_id": Location.objects.get(name="Nairobi Industrial Area").pk,
            "delivery_deadline": str(today + timedelta(days=7)), "min_quality_grade": "grade_1",
            "intended_application": "Mango jam production",
            "packaging_requirements": "20 kg crates", "delivery_arrangement": "supplier_delivers",
            "target_budget": "80000"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        req_id = r.data["id"]
        self.assertRegex(r.data["reference"], r"^SR-\d{4}-\d{6}$")
        self.assertTrue(r.data["matching"]["fully_covered"])

        # 2-5. Ranked matches
        matches = buyer.get(f"/api/v1/sourcing-requests/{req_id}/matches/").data
        self.assertGreaterEqual(len(matches), 3)
        self.assertEqual([m["rank"] for m in matches], sorted(m["rank"] for m in matches))

        # 6. Contact suppliers
        r = buyer.post(f"/api/v1/sourcing-requests/{req_id}/notify-suppliers/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        makueni = next(m for m in matches if m["supplier_name"].startswith("Makueni"))
        r = buyer.post(f"/api/v1/sourcing-requests/{req_id}/contact/",
                       {"supplier_user_id": makueni["supplier_user_id"], "message": "Can you supply?"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        convo_id = r.data["id"]
        r = api("makueni_coop").post(f"/api/v1/conversations/{convo_id}/send/", {"body": "Yes!"}, format="json")
        self.assertEqual(r.status_code, 201)

        # 7. Supplier opportunities, availability and quotations
        plan = {"makueni_coop": ("800", "45", "4000"), "machakos_agg": ("600", "50", "2500"),
                "muranga_farm": ("300", "60", "1500")}
        quote_ids = {}
        for username, (qty, price, delivery) in plan.items():
            client = api(username)
            opp = client.get("/api/v1/matches/").data["results"][0]
            self.assertEqual(opp["sourcing_request"], req_id)
            r = client.post(f"/api/v1/matches/{opp['id']}/respond/", {"available": True, "quantity": qty},
                            format="json")
            self.assertEqual(r.data["status"], "availability_confirmed")
            r = client.post("/api/v1/quotations/", {
                "sourcing_request": req_id, "inventory_item": opp["inventory_item"], "quantity": qty,
                "price_per_unit": price, "delivery_cost": delivery, "includes_delivery": True,
                "earliest_delivery_date": str(today + timedelta(days=3)),
                "valid_until": str(today + timedelta(days=5))}, format="json")
            self.assertEqual(r.status_code, 201, r.content)
            quote_ids[username] = r.data["id"]

        # Negotiation: counter on Kandara to 100 kg, supplier accepts
        r = buyer.post(f"/api/v1/quotations/{quote_ids['muranga_farm']}/counter/",
                       {"quantity": "100", "note": "Only need 100 kg"}, format="json")
        self.assertEqual(r.data["status"], "countered")
        r = api("muranga_farm").post(f"/api/v1/quotations/{quote_ids['muranga_farm']}/revise/",
                                     {"accept_counter": True}, format="json")
        self.assertEqual(r.data["status"], "submitted")
        self.assertEqual(Decimal(r.data["quantity"]), Decimal("100"))

        # Compare offers
        offers = buyer.get(f"/api/v1/sourcing-requests/{req_id}/offers/").data
        self.assertEqual(len(offers["offers"]), 3)
        self.assertEqual(Decimal(offers["cheapest_plan_quantity"]), Decimal("1500"))
        landed = [Decimal(o["landed_unit_cost"]) for o in offers["offers"]]
        self.assertEqual(landed, sorted(landed))

        # 8-9. Accept all three, place a multi-supplier order, suppliers reconfirm
        for qid in quote_ids.values():
            r = buyer.post(f"/api/v1/quotations/{qid}/accept/")
            self.assertEqual(r.status_code, 200, r.content)
        r = buyer.post("/api/v1/orders/", {"sourcing_request": req_id, "quotations": list(quote_ids.values())},
                       format="json")
        self.assertEqual(r.status_code, 201, r.content)
        order_id = r.data["id"]
        self.assertEqual(r.data["status"], "pending_confirmation")
        self.assertEqual(len(r.data["lines"]), 3)

        stock_before = InventoryItem.objects.get(supplier__user__username="makueni_coop").quantity_available
        for line in r.data["lines"]:
            username = OrderLine.objects.get(pk=line["id"]).supplier.user.username
            resp = api(username).post(f"/api/v1/order-lines/{line['id']}/reconfirm/", {"available": True},
                                      format="json")
            self.assertEqual(resp.data["status"], "confirmed", resp.content)
        stock_after = InventoryItem.objects.get(supplier__user__username="makueni_coop").quantity_available
        self.assertEqual(stock_before - stock_after, Decimal("800"), "stock must be reserved on reconfirmation")
        order = buyer.get(f"/api/v1/orders/{order_id}/").data
        self.assertEqual(order["status"], "confirmed")
        self.assertEqual(SourcingRequest.objects.get(pk=req_id).status, "supply_confirmed")

        # 10. Inspection then delivery
        qa = api("qa_officer")
        line_ids = [line["id"] for line in order["lines"]]
        first_line = OrderLine.objects.get(pk=line_ids[0])
        delivery = first_line.delivery
        actor = api(first_line.supplier.user.username)
        r = actor.post(f"/api/v1/deliveries/{delivery.pk}/update_status/", {"status": "scheduled"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = actor.post(f"/api/v1/deliveries/{delivery.pk}/update_status/", {"status": "picked_up"}, format="json")
        self.assertEqual(r.status_code, 409, "pickup must be blocked before inspection")
        self.assertEqual(r.data["error"]["code"], "inspection_required")

        for lid in line_ids:
            r = qa.post(f"/api/v1/order-lines/{lid}/inspect/", {
                "stage": "pre_dispatch", "result": "passed", "grade_observed": "grade_1",
                "notes": "OK"}, format="json")
            self.assertEqual(r.status_code, 201, r.content)

        logistics = user("swift_logistics")
        r = buyer.post(f"/api/v1/deliveries/{OrderLine.objects.get(pk=line_ids[1]).delivery.pk}/assign/",
                       {"logistics_partner": logistics.pk}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

        for lid in line_ids:
            line = OrderLine.objects.get(pk=lid)
            d = line.delivery
            username = d.logistics_partner.username if d.logistics_partner else line.supplier.user.username
            client = api(username)
            for status in (["scheduled"] if d.status == Delivery.Status.PENDING else []) + ["picked_up",
                                                                                           "in_transit"]:
                r = client.post(f"/api/v1/deliveries/{d.pk}/update_status/", {"status": status}, format="json")
                self.assertEqual(r.status_code, 200, (status, r.content))
            r = client.post(f"/api/v1/deliveries/{d.pk}/update_status/", {"status": "delivered"}, format="json")
            self.assertEqual(r.status_code, 400, "delivery must record received quantity")
            r = client.post(f"/api/v1/deliveries/{d.pk}/update_status/", {
                "status": "delivered", "received_quantity": str(line.quantity), "received_by": "Stores"},
                format="json")
            self.assertEqual(r.status_code, 200, r.content)

        self.assertEqual(Order.objects.get(pk=order_id).status, "delivered")

        # Review before completion is refused
        r = buyer.post("/api/v1/reviews/", {"order_line": line_ids[0], "rating": 5}, format="json")
        self.assertEqual(r.status_code, 409)

        # 11. Completion and feedback
        for lid in line_ids:
            r = buyer.post(f"/api/v1/order-lines/{lid}/complete/")
            self.assertEqual(r.data["status"], "completed", r.content)
            r = buyer.post("/api/v1/reviews/", {"order_line": lid, "rating": 5, "comment": "Great"}, format="json")
            self.assertEqual(r.status_code, 201, r.content)
        r = buyer.post("/api/v1/reviews/", {"order_line": line_ids[0], "rating": 4}, format="json")
        self.assertEqual(r.status_code, 409, "one review per party per transaction")

        order = buyer.get(f"/api/v1/orders/{order_id}/").data
        self.assertEqual(order["status"], "completed")
        self.assertEqual(SourcingRequest.objects.get(pk=req_id).status, "completed")
        self.assertGreater(len(order["events"]), 10)
        makueni_profile = user("makueni_coop").supplier_profile
        makueni_profile.refresh_from_db()
        self.assertEqual(makueni_profile.completed_orders, 1)
        self.assertEqual(makueni_profile.average_rating, Decimal("5.00"))
        self.assertIsNotNone(makueni_profile.reliability_score)

        # Admin analytics reflect the completed trade
        m = api("admin").get("/api/v1/admin/analytics/").data
        self.assertEqual(m["orders"]["multi_supplier_orders"], 1)
        self.assertGreater(m["orders"]["completed_value_kes"], 0)


class BusinessRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def test_validation_errors_use_envelope(self):
        r = api("mama_jam").post("/api/v1/sourcing-requests/", {"quantity": "-5"}, format="json")
        self.assertEqual(r.status_code, 400)
        details = r.data["error"]["details"]
        for field in ("product", "delivery_location_id", "delivery_deadline", "intended_application", "quantity"):
            self.assertIn(field, details)

    def test_deadline_in_past_rejected(self):
        r = api("mama_jam").post("/api/v1/sourcing-requests/", {
            "product": Product.objects.get(name="Mango").pk, "quantity": "10",
            "delivery_location_id": Location.objects.first().pk,
            "delivery_deadline": str(timezone.localdate() - timedelta(days=1)),
            "intended_application": "jam"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("delivery_deadline", r.data["error"]["details"])

    def test_unverified_buyer_cannot_submit_requests(self):
        r = api("new_juice_co").post("/api/v1/sourcing-requests/", {
            "product": Product.objects.get(name="Mango").pk, "quantity": "10",
            "delivery_location_id": Location.objects.first().pk,
            "delivery_deadline": str(timezone.localdate() + timedelta(days=3)),
            "intended_application": "juice"}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["error"]["code"], "not_verified")

    def test_supplier_cannot_create_request(self):
        r = api("makueni_coop").post("/api/v1/sourcing-requests/", {}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_unverified_supplier_cannot_quote(self):
        req, _ = mango_request()
        item = InventoryItem.objects.get(supplier__user__username="kitui_farm")
        r = api("kitui_farm").post("/api/v1/quotations/", {
            "sourcing_request": req.pk, "inventory_item": item.pk, "quantity": "100", "price_per_unit": "40",
            "earliest_delivery_date": str(timezone.localdate() + timedelta(days=1)),
            "valid_until": str(timezone.localdate() + timedelta(days=3))}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["error"]["code"], "not_verified")

    def test_unmatched_supplier_cannot_quote(self):
        req, _ = mango_request()
        item = InventoryItem.objects.get(supplier__user__username="embu_farm")
        r = api("embu_farm").post("/api/v1/quotations/", {
            "sourcing_request": req.pk, "inventory_item": item.pk, "quantity": "100", "price_per_unit": "40",
            "earliest_delivery_date": str(timezone.localdate() + timedelta(days=1)),
            "valid_until": str(timezone.localdate() + timedelta(days=3))}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_quote_cannot_exceed_stock(self):
        req, _ = mango_request()
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            quote(req, "makueni_coop", "900", "45")  # only 800 in stock

    def test_accepting_beyond_requested_quantity_refused(self):
        req, _ = mango_request(quantity=Decimal("1000"))
        q1 = quote(req, "makueni_coop", "800", "45")
        q2 = quote(req, "machakos_agg", "600", "50")
        buyer = api("mama_jam")
        self.assertEqual(buyer.post(f"/api/v1/quotations/{q1.pk}/accept/").status_code, 200)
        r = buyer.post(f"/api/v1/quotations/{q2.pk}/accept/")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["error"]["code"], "quantity_exceeded")

    def test_reconfirmation_fails_when_stock_sold_elsewhere(self):
        req, _ = mango_request()
        q = quote(req, "makueni_coop", "800", "45")
        api("mama_jam").post(f"/api/v1/quotations/{q.pk}/accept/")
        r = api("mama_jam").post("/api/v1/orders/", {"sourcing_request": req.pk, "quotations": [q.pk]},
                                 format="json")
        line_id = r.data["lines"][0]["id"]
        InventoryItem.objects.filter(pk=q.inventory_item_id).update(quantity_available=Decimal("100"))
        r = api("makueni_coop").post(f"/api/v1/order-lines/{line_id}/reconfirm/", {"available": True},
                                     format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["error"]["code"], "insufficient_stock")
        r = api("makueni_coop").post(f"/api/v1/order-lines/{line_id}/reconfirm/", {"available": False},
                                     format="json")
        self.assertEqual(r.data["status"], "cancelled")
        self.assertEqual(OrderLine.objects.get(pk=line_id).order.status, "cancelled")

    def test_cancel_order_releases_reserved_stock(self):
        req, _ = mango_request()
        q = quote(req, "machakos_agg", "600", "50")
        buyer = api("mama_jam")
        buyer.post(f"/api/v1/quotations/{q.pk}/accept/")
        order = buyer.post("/api/v1/orders/", {"sourcing_request": req.pk, "quotations": [q.pk]},
                           format="json").data
        item = InventoryItem.objects.get(pk=q.inventory_item_id)
        before = item.quantity_available
        api("machakos_agg").post(f"/api/v1/order-lines/{order['lines'][0]['id']}/reconfirm/", {"available": True},
                                 format="json")
        item.refresh_from_db()
        self.assertEqual(item.quantity_available, before - 600)
        r = buyer.post(f"/api/v1/orders/{order['id']}/cancel/")
        self.assertEqual(r.data["status"], "cancelled")
        item.refresh_from_db()
        self.assertEqual(item.quantity_available, before)

    def test_failed_inspection_blocks_line(self):
        req, _ = mango_request()
        q = quote(req, "makueni_coop", "800", "45")
        buyer = api("mama_jam")
        buyer.post(f"/api/v1/quotations/{q.pk}/accept/")
        order = buyer.post("/api/v1/orders/", {"sourcing_request": req.pk, "quotations": [q.pk]},
                           format="json").data
        lid = order["lines"][0]["id"]
        api("makueni_coop").post(f"/api/v1/order-lines/{lid}/reconfirm/", {"available": True}, format="json")
        r = api("qa_officer").post(f"/api/v1/order-lines/{lid}/inspect/",
                                   {"stage": "pre_dispatch", "result": "failed", "notes": "Anthracnose"},
                                   format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(OrderLine.objects.get(pk=lid).status, "inspection_failed")
        self.assertEqual(Order.objects.get(pk=order["id"]).status, "cancelled")

    def test_supplier_cannot_inspect_own_goods(self):
        req, _ = mango_request()
        q = quote(req, "makueni_coop", "800", "45")
        buyer = api("mama_jam")
        buyer.post(f"/api/v1/quotations/{q.pk}/accept/")
        order = buyer.post("/api/v1/orders/", {"sourcing_request": req.pk, "quotations": [q.pk]},
                           format="json").data
        lid = order["lines"][0]["id"]
        api("makueni_coop").post(f"/api/v1/order-lines/{lid}/reconfirm/", {"available": True}, format="json")
        r = api("makueni_coop").post(f"/api/v1/order-lines/{lid}/inspect/",
                                     {"stage": "pre_dispatch", "result": "passed"}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["error"]["code"], "permission_denied")

    def test_disputes_flag_order_and_resolution_is_audited(self):
        req, _ = mango_request()
        q = quote(req, "makueni_coop", "800", "45")
        buyer = api("mama_jam")
        buyer.post(f"/api/v1/quotations/{q.pk}/accept/")
        order = buyer.post("/api/v1/orders/", {"sourcing_request": req.pk, "quotations": [q.pk]},
                           format="json").data
        r = buyer.post(f"/api/v1/orders/{order['id']}/dispute/", {"reason": "Wrong variety"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Order.objects.get(pk=order["id"]).status, "disputed")
        r = api("admin").post(f"/api/v1/disputes/{r.data['id']}/resolve/",
                              {"status": "resolved", "resolution": "Supplier replaced fruit"}, format="json")
        self.assertEqual(r.data["status"], "resolved")
        self.assertEqual(Order.objects.get(pk=order["id"]).status, "pending_confirmation")
        self.assertTrue(AuditLog.objects.filter(action="dispute.resolved").exists())


class PermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def test_anonymous_is_rejected(self):
        self.assertEqual(api().get("/api/v1/sourcing-requests/").status_code, 401)

    def test_buyers_only_see_their_requests(self):
        mango_request()
        self.assertEqual(api("mama_jam").get("/api/v1/sourcing-requests/").data["count"], 1)
        self.assertEqual(api("new_juice_co").get("/api/v1/sourcing-requests/").data["count"], 0)

    def test_suppliers_only_see_requests_they_are_matched_to(self):
        mango_request()
        self.assertEqual(api("makueni_coop").get("/api/v1/sourcing-requests/").data["count"], 1)
        self.assertEqual(api("kitui_farm").get("/api/v1/sourcing-requests/").data["count"], 0)

    def test_suppliers_only_see_own_inventory(self):
        data = api("makueni_coop").get("/api/v1/inventory/").data["results"]
        self.assertTrue(data)
        self.assertTrue(all(i["supplier_name"].startswith("Makueni") for i in data))
        other = InventoryItem.objects.get(supplier__user__username="machakos_agg")
        self.assertEqual(api("makueni_coop").patch(f"/api/v1/inventory/{other.pk}/", {"quantity_available": 1},
                                                   format="json").status_code, 404)

    def test_buyer_cannot_accept_another_buyers_quotation(self):
        req, _ = mango_request()
        q = quote(req, "makueni_coop", "800", "45")
        self.assertEqual(api("new_juice_co").post(f"/api/v1/quotations/{q.pk}/accept/").status_code, 404)

    def test_admin_endpoints_require_admin(self):
        for path in ("/api/v1/admin/analytics/", "/api/v1/admin/audit-logs/", "/api/v1/admin/matching-config/",
                     "/api/v1/admin/suppliers/"):
            self.assertEqual(api("mama_jam").get(path).status_code, 403, path)
            self.assertEqual(api("admin").get(path).status_code, 200, path)

    def test_verification_is_audited_and_enables_matching(self):
        kitui = user("kitui_farm").supplier_profile
        r = api("admin").post(f"/api/v1/admin/suppliers/{kitui.pk}/verify/", {"status": "approved"}, format="json")
        self.assertEqual(r.data["verification_status"], "approved")
        self.assertTrue(AuditLog.objects.filter(action="verification.approved", target_id=str(kitui.pk)).exists())
        req, _ = mango_request()
        self.assertTrue(req.matches.filter(supplier=kitui).exists())

    def test_matching_config_update_is_audited(self):
        r = api("admin").patch("/api/v1/admin/matching-config/", {"weight_proximity": "0.5"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="config.matching_updated").exists())

    def test_audit_log_is_immutable(self):
        entry = AuditLog.objects.create(action="test")
        with self.assertRaises(ValueError):
            entry.save()
        with self.assertRaises(ValueError):
            entry.delete()

    def test_conversation_only_with_matched_suppliers(self):
        req, _ = mango_request()
        r = api("mama_jam").post(f"/api/v1/sourcing-requests/{req.pk}/contact/",
                                 {"supplier_user_id": user("kitui_farm").pk, "message": "hi"}, format="json")
        self.assertEqual(r.status_code, 409)


class AuthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def test_register_login_and_me(self):
        loc = Location.objects.first()
        r = api().post("/api/v1/auth/register/", {
            "username": "wanjiku", "email": "w@example.com", "password": "Strong-pass-254",
            "role": "farmer", "phone": "+254722000111",
            "profile": {"business_name": "Wanjiku Farm", "location_id": loc.pk, "can_deliver": True,
                        "delivery_radius_km": 50}}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data["profile"]["verification_status"], "approved", "no verification step by default")
        self.assertTrue(r.data["is_verified"])
        r = api().post("/api/v1/auth/token/", {"username": "wanjiku", "password": "Strong-pass-254"}, format="json")
        self.assertEqual(r.status_code, 200)
        client = api()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        me = client.get("/api/v1/auth/me/").data
        self.assertEqual(me["role"], "farmer")

    @override_settings(REQUIRE_ACCOUNT_VERIFICATION=True)
    def test_registration_waits_for_approval_when_verification_enabled(self):
        r = api().post("/api/v1/auth/register/", {
            "username": "kamau", "email": "k@example.com", "password": "Strong-pass-254", "role": "processor",
            "profile": {"business_name": "Kamau Foods", "location_id": Location.objects.first().pk}}, format="json")
        self.assertEqual(r.data["profile"]["verification_status"], "pending")
        self.assertFalse(r.data["is_verified"])

    def test_web_registration_is_ready_to_use(self):
        from django.test import Client
        c = Client()
        r = c.post("/register/", {
            "role": "farmer", "business_name": "Achieng Farm", "first_name": "Achieng", "last_name": "O",
            "username": "achieng", "email": "a@example.com", "phone": "+254700111222",
            "location": Location.objects.first().pk, "password": "Strong-pass-254"}, follow=True)
        self.assertContains(r, "Your account is active")
        u = user("achieng")
        self.assertTrue(u.is_verified)
        # and can list stock that buyers are matched to straight away
        self.assertEqual(u.supplier_profile.verification_status, "approved")

    def test_cannot_self_register_as_admin(self):
        r = api().post("/api/v1/auth/register/", {
            "username": "eve", "email": "eve@example.com", "password": "Strong-pass-254", "role": "admin",
            "profile": {}}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_weak_password_rejected(self):
        r = api().post("/api/v1/auth/register/", {
            "username": "bob", "email": "bob@example.com", "password": "password", "role": "processor",
            "profile": {"business_name": "Bob", "location_id": Location.objects.first().pk}}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_web_pages_render_for_each_role(self):
        from django.test import Client
        req, _ = mango_request()
        for username, path in (("mama_jam", f"/app/requests/{req.pk}/"), ("makueni_coop", "/app/opportunities/"),
                               ("swift_logistics", "/app/deliveries/"), ("admin", "/app/admin/")):
            c = Client()
            c.force_login(user(username))
            self.assertEqual(c.get(path).status_code, 200, (username, path))
        self.assertEqual(Client().get("/").status_code, 200)
        self.assertEqual(Client().get("/app/").status_code, 302)
        match = Match.objects.filter(sourcing_request=req, supplier__user__username="makueni_coop").first()
        c = Client()
        c.force_login(user("makueni_coop"))
        self.assertEqual(c.get(f"/app/opportunities/{match.pk}/").status_code, 200)
