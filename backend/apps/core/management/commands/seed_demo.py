"""Load demonstration data: Kenyan locations, catalogue, verified users and inventory.

    python manage.py seed_demo            # idempotent
    python manage.py seed_demo --reset    # wipe transactional data first

All demo accounts use the password printed at the end.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import (BuyerProfile, LogisticsProfile, Role, SupplierProfile, User,
                                  VerificationStatus)
from apps.catalog.models import Category, InventoryItem, Product, ProductVariety, QualityGrade
from apps.core.models import Location, MatchingConfig

DEMO_PASSWORD = "Origin254!demo"

LOCATIONS = {
    "nairobi": ("Nairobi Industrial Area", "Nairobi", "Embakasi", -1.3080, 36.8510),
    "machakos": ("Machakos Fresh Produce Market", "Machakos", "Machakos Town", -1.5177, 37.2634),
    "makueni": ("Kibwezi", "Makueni", "Kibwezi West", -2.4166, 37.9667),
    "muranga": ("Kenol", "Murang'a", "Kandara", -0.9050, 37.1300),
    "embu": ("Embu Town", "Embu", "Manyatta", -0.5390, 37.4570),
    "kitui": ("Kitui Town", "Kitui", "Kitui Central", -1.3667, 38.0106),
    "meru": ("Meru Town", "Meru", "Imenti North", 0.0470, 37.6490),
    "kilifi": ("Malindi Collection Centre", "Kilifi", "Malindi", -3.2192, 40.1169),
    "kisumu": ("Kisumu City", "Kisumu", "Kisumu Central", -0.0917, 34.7680),
    "eldoret": ("Eldoret", "Uasin Gishu", "Kapseret", 0.5143, 35.2698),
}

CATALOGUE = {
    "Fruits": {
        "Mango": (["Apple", "Ngowe", "Kent", "Tommy Atkins", "Van Dyke"], []),
        "Pineapple": (["Smooth Cayenne", "MD2"], ["weight_kg"]),
        "Passion fruit": (["Purple", "Yellow"], ["juice_pct"]),
        "Avocado": (["Hass", "Fuerte"], ["dry_matter_pct"]),
    },
    "Vegetables": {
        "Tomato": (["Anna F1", "Kilele F1"], ["firmness"]),
        "Onion": (["Red Creole"], ["size_mm"]),
    },
    "Grains & cereals": {
        "Maize": (["White", "Yellow"], ["moisture_pct", "aflatoxin_ppb"]),
        "Sorghum": (["Gadam"], ["moisture_pct"]),
    },
    "Legumes": {
        "Beans": (["Rosecoco", "Mwitemania"], ["moisture_pct"]),
        "Groundnuts": (["Red Valencia"], ["moisture_pct", "aflatoxin_ppb"]),
    },
}

# (username, role, business, location, verification, can_deliver, radius, reliability, inventory[])
# inventory: (variety, kg, grade, price, attributes, days_since_confirmed, can_deliver)
SUPPLIERS = [
    ("makueni_coop", Role.FARMER, "Makueni Mango Growers", "makueni", "approved", True, 300,
     "0.880", [("Apple", 800, QualityGrade.GRADE_1, 45, {}, 1, True)]),
    ("machakos_agg", Role.FARMER, "Machakos Fresh Produce Farm", "machakos", "approved", True, 150,
     "0.760", [("Ngowe", 600, QualityGrade.GRADE_1, 50, {}, 2, True)]),
    ("muranga_farm", Role.FARMER, "Kandara Orchards (J. Wanjiru)", "muranga", "approved", True, 120,
     None, [("Apple", 300, QualityGrade.PREMIUM, 60, {}, 0, True)]),
    ("kilifi_coop", Role.FARMER, "Malindi Coast Mango Farm", "kilifi", "approved", False, 0,
     "0.700", [("Kent", 1200, QualityGrade.GRADE_1, 38, {}, 3, False)]),
    ("embu_farm", Role.FARMER, "Embu Highland Farm", "embu", "approved", True, 200,
     "0.650", [("Apple", 400, QualityGrade.GRADE_2, 35, {}, 1, True)]),
    ("kitui_farm", Role.FARMER, "Kitui Dryland Fruits", "kitui", "pending", True, 200,
     None, [("Ngowe", 900, QualityGrade.GRADE_1, 40, {}, 1, True)]),
    ("meru_farm", Role.FARMER, "Meru Sunrise Farm", "meru", "approved", True, 250,
     "0.720", [("Tommy Atkins", 700, QualityGrade.GRADE_1, 42, {}, 30, True)]),
    ("kisumu_agg", Role.FARMER, "Lake Region Fruit Farm", "kisumu", "approved", True, 100,
     "0.800", [("Apple", 1000, QualityGrade.GRADE_1, 40, {}, 1, True)]),
]


class Command(BaseCommand):
    help = "Load Origin 254 demonstration data (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Delete sourcing requests, orders, messages and notifications first.")

    @transaction.atomic
    def handle(self, *args, **opts):
        if opts["reset"]:
            self._reset()
        MatchingConfig.get()
        call_command("load_kenya_locations", stdout=self.stdout)
        locs = {key: Location.objects.update_or_create(
            name=name, county=county,
            defaults={"sub_county": sub, "latitude": Decimal(str(lat)), "longitude": Decimal(str(lng))})[0]
            for key, (name, county, sub, lat, lng) in LOCATIONS.items()}

        for cat_name, products in CATALOGUE.items():
            cat, _ = Category.objects.get_or_create(name=cat_name)
            for prod_name, (varieties, params) in products.items():
                prod, _ = Product.objects.update_or_create(
                    name=prod_name, defaults={"category": cat, "quality_parameters": params,
                                              "perishable": cat_name in {"Fruits", "Vegetables"}})
                for v in varieties:
                    ProductVariety.objects.get_or_create(product=prod, name=v)

        admin = self._user("admin", Role.ADMIN, "admin@origin254.example", "Platform", "Admin",
                           is_staff=True, is_superuser=True)
        self._user("qa_officer", Role.ADMIN, "qa@origin254.example", "Grace", "Mutua", is_staff=True)

        buyer = self._user("mama_jam", Role.PROCESSOR, "orders@mamajam.example", "Achieng", "Otieno",
                           phone="+254700000001")
        BuyerProfile.objects.update_or_create(user=buyer, defaults={
            "business_name": "Mama Jam Foods Ltd", "business_type": "Fruit jam & preserves processor",
            "location": locs["nairobi"], "monthly_volume_kg": 6000, "registration_number": "PVT-2021-88341",
            "verification_status": VerificationStatus.APPROVED, "verified_at": timezone.now(), "verified_by": admin})

        mango = Product.objects.get(name="Mango")
        now = timezone.now()
        for i, (username, role, business, loc, status, delivers, radius, reliability, stock) in enumerate(SUPPLIERS):
            user = self._user(username, role, f"{username}@suppliers.example", business.split()[0], "Supplier",
                              phone=f"+2547110000{i:02d}")
            profile, _ = SupplierProfile.objects.update_or_create(user=user, defaults={
                "business_name": business, "location": locs[loc], "can_deliver": delivers,
                "delivery_radius_km": radius, "verification_status": status,
                "verified_at": now if status == "approved" else None,
                "verified_by": admin if status == "approved" else None,
                "reliability_score": Decimal(reliability) if reliability else None,
                "member_count": None,
                "certifications": "KEBS" if username.endswith(("_coop", "_agg")) else ""})
            for variety, kg, grade, price, attrs, age_days, item_delivers in stock:
                item, _ = InventoryItem.objects.update_or_create(
                    supplier=profile, product=mango, variety=ProductVariety.objects.get(product=mango, name=variety),
                    defaults={"quantity_available": kg, "unit": "kg", "quality_grade": grade,
                              "price_per_unit": price, "quality_attributes": attrs, "location": locs[loc],
                              "harvest_date": timezone.localdate() - timedelta(days=2),
                              "available_from": timezone.localdate(),
                              "available_until": timezone.localdate() + timedelta(days=21),
                              "can_deliver": item_delivers, "packaging": "Plastic crates", "is_active": True,
                              "last_confirmed_at": now - timedelta(days=age_days)})
                self._demo_photos(item, i)

        logi = self._user("swift_logistics", Role.LOGISTICS, "dispatch@swiftagri.example", "Peter", "Kamau",
                          phone="+254700000009")
        LogisticsProfile.objects.update_or_create(user=logi, defaults={
            "business_name": "Swift Agri Logistics", "location": locs["nairobi"], "vehicle_capacity_kg": 5000,
            "refrigerated": True, "coverage_counties": "Nairobi, Machakos, Makueni, Murang'a, Kiambu",
            "verification_status": VerificationStatus.APPROVED, "verified_at": now, "verified_by": admin})

        # A pending buyer to show the verification queue.
        pending = self._user("new_juice_co", Role.PROCESSOR, "hello@newjuice.example", "Brian", "Kiptoo")
        BuyerProfile.objects.update_or_create(user=pending, defaults={
            "business_name": "Highland Juice Co.", "business_type": "Juice processor", "location": locs["eldoret"],
            "verification_status": VerificationStatus.PENDING})

        self.stdout.write(self.style.SUCCESS("Demo data loaded."))
        self.stdout.write(f"Password for every demo account: {DEMO_PASSWORD}")
        self.stdout.write("Accounts: admin, qa_officer, mama_jam (buyer), makueni_coop, machakos_agg, muranga_farm, "
                          "kilifi_coop, embu_farm, kitui_farm (pending), meru_farm (stale stock), kisumu_agg, "
                          "swift_logistics, new_juice_co (pending buyer)")

    def _demo_photos(self, item, seed):
        """Give demo listings the required minimum of product photos (generated placeholders)."""
        from django.conf import settings
        from django.core.files.base import ContentFile

        from apps.catalog.models import InventoryPhoto
        from apps.core.demo_images import DEMO_PALETTES, placeholder_png

        for n in range(item.photos.count(), settings.MIN_PHOTOS_PER_LISTING):
            top, bottom = DEMO_PALETTES[(seed + n) % len(DEMO_PALETTES)]
            photo = InventoryPhoto(inventory_item=item, caption=f"Demo photo {n + 1} (placeholder)")
            photo.image.save(f"demo-{item.pk}-{n + 1}.png", ContentFile(placeholder_png(top, bottom)), save=False)
            photo.save()

    def _user(self, username, role, email, first, last, phone="", **extra):
        user, created = User.objects.get_or_create(username=username, defaults={
            "role": role, "email": email, "first_name": first, "last_name": last, "phone": phone, **extra})
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save()
        return user

    def _reset(self):
        from apps.comms.models import Conversation, Notification
        from apps.orders.models import Order, SaleRecord
        from apps.reviews.models import Review
        from apps.sourcing.models import SourcingRequest

        Review.objects.all().delete()
        from apps.payments.models import Payment, Payout
        Payout.objects.all().delete()
        Payment.objects.all().delete()
        SaleRecord.objects.all().delete()
        Order.objects.all().delete()
        SourcingRequest.objects.all().delete()
        Conversation.objects.all().delete()
        Notification.objects.all().delete()
        SupplierProfile.objects.update(completed_orders=0, average_rating=None, on_time_rate=None,
                                       inspection_pass_rate=None)
        self.stdout.write("Transactional data cleared.")
