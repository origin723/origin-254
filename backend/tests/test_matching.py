from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import SupplierProfile, VerificationStatus
from apps.catalog.models import InventoryItem, QualityGrade
from apps.core.geo import haversine_km, location_ids_within
from apps.core.models import Location, MatchingConfig
from apps.sourcing.matching import run_matching
from apps.sourcing.models import Match

from .factories import mango_request, seed


class MatchingEngineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    def suppliers(self, req):
        return list(req.matches.order_by("rank").values_list("supplier__user__username", flat=True))

    def test_only_approved_fresh_qualifying_suppliers_are_matched(self):
        req, result = mango_request()
        names = self.suppliers(req)
        self.assertIn("makueni_coop", names)
        self.assertIn("machakos_agg", names)
        self.assertNotIn("kitui_farm", names, "pending verification must be excluded")
        self.assertNotIn("meru_farm", names, "stale inventory must be excluded")
        self.assertNotIn("embu_farm", names, "grade 2 below a grade 1 requirement must be excluded")
        self.assertTrue(all(m.supplier.verification_status == VerificationStatus.APPROVED
                            for m in req.matches.all()))

    def test_ranking_is_by_descending_score(self):
        req, _ = mango_request()
        scores = list(req.matches.order_by("rank").values_list("score", flat=True))
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(list(req.matches.order_by("rank").values_list("rank", flat=True)),
                         list(range(1, len(scores) + 1)))

    def test_multi_supplier_allocation_covers_request(self):
        req, result = mango_request()
        self.assertTrue(result.fully_covered)
        self.assertGreater(result.suppliers_needed, 1, "no single supplier has 1,500 kg")
        allocated = sum(m.suggested_quantity for m in req.matches.all())
        self.assertEqual(allocated, Decimal("1500"))
        for m in req.matches.all():
            self.assertLessEqual(m.suggested_quantity, m.available_quantity)

    def test_partial_coverage_reported_when_supply_is_short(self):
        req, result = mango_request(quantity=Decimal("50000"))
        self.assertFalse(result.fully_covered)
        self.assertLess(result.coverage, 1)

    def test_deadline_excludes_distant_suppliers(self):
        # Malindi is ~420 km away: with today's deadline and 400 km/day, it cannot arrive in time.
        req, result = mango_request(delivery_deadline=timezone.localdate())
        self.assertNotIn("kilifi_coop", self.suppliers(req))

    def test_max_distance_filter(self):
        config = MatchingConfig.get()
        config.max_distance_km = 100
        config.save()
        req, _ = mango_request()
        for m in req.matches.all():
            self.assertLessEqual(m.distance_km, 100)
        self.assertNotIn("makueni_coop", self.suppliers(req))

    def test_weights_change_ranking(self):
        config = MatchingConfig.get()
        for f in ("product", "quantity", "quality", "delivery", "reliability"):
            setattr(config, f"weight_{f}", Decimal("0"))
        config.weight_proximity = Decimal("1")
        config.save()
        req, _ = mango_request()
        distances = list(req.matches.order_by("rank").values_list("distance_km", flat=True))
        self.assertEqual(distances, sorted(distances))

    def test_one_grade_below_allowed_when_configured(self):
        config = MatchingConfig.get()
        config.allow_one_grade_below = True
        config.save()
        req, _ = mango_request()
        embu = req.matches.get(supplier__user__username="embu_farm")
        self.assertLess(embu.score_breakdown["components"]["quality"], 0.5)

    def test_spec_shortfall_penalised(self):
        InventoryItem.objects.filter(supplier__user__username="machakos_agg").update(quality_attributes={"size_mm": 70})
        InventoryItem.objects.filter(supplier__user__username="makueni_coop").update(quality_attributes={"size_mm": 80})
        req, _ = mango_request(quality_specifications={"min_size_mm": 75})
        machakos = req.matches.get(supplier__user__username="machakos_agg")
        makueni = req.matches.get(supplier__user__username="makueni_coop")
        self.assertLess(machakos.score_breakdown["components"]["quality"],
                        makueni.score_breakdown["components"]["quality"])

    def test_rematch_keeps_engaged_matches_and_drops_unqualified(self):
        req, _ = mango_request()
        contacted = req.matches.get(supplier__user__username="makueni_coop")
        contacted.status = Match.Status.CONTACTED
        contacted.save()
        SupplierProfile.objects.filter(user__username="machakos_agg").update(
            verification_status=VerificationStatus.SUSPENDED)
        SupplierProfile.objects.filter(user__username="makueni_coop").update(
            verification_status=VerificationStatus.SUSPENDED)
        run_matching(req)
        self.assertFalse(req.matches.filter(supplier__user__username="machakos_agg").exists())
        contacted.refresh_from_db()
        self.assertEqual(contacted.status, Match.Status.EXPIRED)

    def test_new_inventory_is_picked_up_on_rematch(self):
        req, _ = mango_request()
        embu = InventoryItem.objects.get(supplier__user__username="embu_farm")
        embu.quality_grade = QualityGrade.GRADE_1
        embu.last_confirmed_at = timezone.now()
        embu.save()
        run_matching(req)
        self.assertIn("embu_farm", self.suppliers(req))

    def test_buyer_cannot_match_against_own_inventory(self):
        req, _ = mango_request()
        self.assertFalse(req.matches.filter(supplier__user=req.buyer).exists())


class GeoTests(TestCase):
    def test_haversine_nairobi_machakos(self):
        self.assertAlmostEqual(haversine_km((-1.2921, 36.8219), (-1.5177, 37.2634)), 54.9, delta=1)

    def test_radius_query(self):
        a = Location.objects.create(name="A", county="Nairobi", latitude=Decimal("-1.30"), longitude=Decimal("36.85"))
        b = Location.objects.create(name="B", county="Mombasa", latitude=Decimal("-4.04"), longitude=Decimal("39.67"))
        ids = location_ids_within(-1.29, 36.82, 50)
        self.assertIn(a.pk, ids)
        self.assertNotIn(b.pk, ids)
