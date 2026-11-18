"""Populate a realistic Kenyan marketplace: 10 users per role (plus 3 beef ranchers), produce listings with
real product photos, sourcing requests, quotations, approvals and orders.

    python manage.py seed_kenya                      # idempotent: safe to re-run
    python manage.py seed_kenya --password 'S3cret…' # password for newly created accounts

Everything goes through the real service layer (verification, matching, quotations,
orders), so notifications, audit entries and statuses are exactly what real use creates.
New accounts get one random password (printed once) unless --password is given;
existing accounts are never changed. Photos come from demo_assets/product_photos
(Wikimedia Commons, see CREDITS.md there) and are saved to the configured media storage.
"""
import hashlib
import json
import secrets
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import BuyerProfile, LogisticsProfile, Role, SupplierProfile, User, VerificationStatus
from apps.accounts.services import set_verification
from apps.catalog.models import Category, InventoryItem, InventoryPhoto, Product, ProductVariety, QualityGrade
from apps.core.exceptions import WorkflowError
from apps.core.models import Location, MatchingConfig
from apps.orders import services as orders
from apps.sourcing import services as sourcing
from apps.sourcing.models import DeliveryArrangement, SourcingRequest

DEMO_TAG = "[Kenya demo]"
EMAIL_DOMAIN = "demo.origin254.example"
G1, G2, PREM, PROC = QualityGrade.GRADE_1, QualityGrade.GRADE_2, QualityGrade.PREMIUM, QualityGrade.PROCESSING

# product: (category, photo slug, varieties, perishable)
CATALOGUE = {
    "Mango": ("Fruits", "mango", ["Apple", "Ngowe", "Kent", "Tommy Atkins"], True),
    "Avocado": ("Fruits", "avocado", ["Hass", "Fuerte"], True),
    "Pineapple": ("Fruits", "pineapple", ["Smooth Cayenne", "MD2"], True),
    "Passion fruit": ("Fruits", "passion-fruit", ["Purple", "Yellow"], True),
    "Banana": ("Fruits", "banana", ["Cavendish", "Matoke", "Kampala"], True),
    "Tomato": ("Vegetables", "tomato", ["Anna F1", "Kilele F1", "Rio Grande"], True),
    "Onion": ("Vegetables", "onion", ["Red Creole", "Bombay Red"], True),
    "Sukuma wiki": ("Vegetables", "sukuma-wiki", ["Collards", "Thousand-headed kale"], True),
    "Cabbage": ("Vegetables", "cabbage", ["Gloria F1", "Copenhagen"], True),
    "Carrot": ("Vegetables", "carrot", ["Nantes", "Chantenay"], True),
    "Irish potato": ("Roots & tubers", "irish-potato", ["Shangi", "Dutch Robjin", "Unica"], True),
    "Sweet potato": ("Roots & tubers", "sweet-potato", ["Kabode (orange-fleshed)", "Bungoma"], True),
    "Cassava": ("Roots & tubers", "cassava", ["Kibandameno", "Tajirika"], True),
    "Maize": ("Grains & cereals", "maize", ["White", "Yellow"], False),
    "Sorghum": ("Grains & cereals", "sorghum", ["Gadam", "Serena"], False),
    "Rice": ("Grains & cereals", "rice", ["Pishori (Mwea)", "Basmati 370"], False),
    "Beans": ("Legumes", "beans", ["Rosecoco", "Mwitemania", "Nyayo"], False),
    "Green grams": ("Legumes", "green-grams", ["Ndengu N26", "Blackgram"], False),
    "Groundnuts": ("Legumes", "groundnuts", ["Red Valencia", "Homa Bay local"], False),
    "Macadamia": ("Nuts & beverage crops", "macadamia", ["Murang'a 20", "Kiambu 3"], False),
    "Coffee": ("Nuts & beverage crops", "coffee", ["SL28 cherry", "Ruiru 11 cherry"], False),
    "Beef": ("Meat & livestock", "beef", ["Boran (grass-fed)", "Sahiwal", "Boran cross"], True),
}

# (first, last, business, constituency, county, delivery radius km,
#  [(product, variety, kg, grade, KES/kg)])
FARMERS = [
    ("Grace", "Wanjiku", "Wanjiku Orchards", "Kandara", "Murang'a", 200,
     [("Avocado", "Hass", 2500, PREM, 65), ("Mango", "Apple", 1500, G1, 45), ("Macadamia", "Murang'a 20", 800, G1, 190)]),
    ("Peter", "Ochieng", "Ochieng Ahero Rice Farm", "Nyando", "Kisumu", 250,
     [("Rice", "Pishori (Mwea)", 4000, G1, 150), ("Sorghum", "Gadam", 3000, G1, 50), ("Sweet potato", "Bungoma", 1500, G2, 40)]),
    ("Kiprono", "Kipchumba", "Kipchumba Highland Farm", "Kapseret", "Uasin Gishu", 300,
     [("Maize", "White", 9000, G1, 45), ("Beans", "Rosecoco", 2500, G1, 120), ("Irish potato", "Dutch Robjin", 4000, G1, 42)]),
    ("Mary", "Njeri", "Njeri Ol Kalou Growers", "Ol Kalou", "Nyandarua", 250,
     [("Irish potato", "Shangi", 6000, G1, 45), ("Cabbage", "Gloria F1", 3000, G1, 25), ("Carrot", "Nantes", 2000, G1, 50)]),
    ("Mutua", "Musyoka", "Musyoka Dryland Produce", "Kibwezi West", "Makueni", 300,
     [("Mango", "Ngowe", 3000, G1, 40), ("Green grams", "Ndengu N26", 2000, G1, 140), ("Mango", "Kent", 1200, PREM, 55)]),
    ("Fatuma", "Hassan", "Pwani Mixed Farm", "Malindi", "Kilifi", 200,
     [("Cassava", "Kibandameno", 5000, G1, 30), ("Mango", "Tommy Atkins", 2000, G1, 38), ("Pineapple", "Smooth Cayenne", 2500, G1, 45)]),
    ("Chebet", "Rotich", "Chebet Fresh Greens", "Ainamoi", "Kericho", 150,
     [("Sukuma wiki", "Collards", 1500, G1, 30), ("Tomato", "Kilele F1", 2500, G1, 70), ("Onion", "Red Creole", 2000, G1, 90)]),
    ("Wekesa", "Barasa", "Barasa Western Granary", "Kanduyi", "Bungoma", 300,
     [("Maize", "Yellow", 7000, G1, 44), ("Beans", "Mwitemania", 2000, G2, 110), ("Banana", "Matoke", 3000, G1, 30)]),
    ("Akinyi", "Achieng", "Lakeside Groundnut Growers", "Homa Bay Town", "Homa Bay", 250,
     [("Groundnuts", "Red Valencia", 2500, G1, 160), ("Sorghum", "Serena", 2500, G2, 48), ("Sweet potato", "Kabode (orange-fleshed)", 2000, G1, 45)]),
    ("Kariuki", "Ndirangu", "Ndirangu Thika Farms", "Gatundu South", "Kiambu", 400,
     [("Pineapple", "MD2", 4000, PREM, 55), ("Passion fruit", "Purple", 1500, G1, 120), ("Coffee", "SL28 cherry", 3000, G1, 85)]),
    # Ranchers: beef is their top product.
    ("Lemayian", "Sankale", "Sankale Ranch", "Narok North", "Narok", 300,
     [("Beef", "Boran (grass-fed)", 1500, PREM, 600), ("Maize", "White", 3000, G1, 45)]),
    ("Wairimu", "Gitau", "Gitau Laikipia Ranch", "Laikipia West", "Laikipia", 300,
     [("Beef", "Sahiwal", 1200, G1, 560), ("Irish potato", "Shangi", 2000, G1, 44)]),
    ("Saitoti", "Koileken", "Kajiado Plains Beef", "Kajiado Central", "Kajiado", 250,
     [("Beef", "Boran cross", 2000, G1, 550), ("Onion", "Bombay Red", 1500, G1, 85)]),
]

# (first, last, business, business type, constituency, county, monthly kg,
#  [(product, kg, min grade, use, packaging, budget KES)])
PROCESSORS = [
    ("Wambui", "Njoroge", "Nyota Juice Ltd", "Fruit juice & nectar processor", "Embakasi East", "Nairobi", 20000,
     [("Mango", 2000, G1, "Mango nectar production", "Plastic crates, max 20 kg", 90000),
      ("Passion fruit", 800, G1, "Passion juice concentrate", "Nets of 10 kg", 100000)]),
    ("James", "Muriuki", "Mwea Grain Millers", "Rice miller & packer", "Mwea", "Kirinyaga", 60000,
     [("Rice", 3000, G1, "Packed Pishori rice, 2 kg packs", "50 kg woven bags", 470000),
      ("Sorghum", 2000, G2, "Sorghum flour blend", "90 kg bags", 105000)]),
    ("Amina", "Said", "Pwani Cassava Flour", "Cassava flour & crisps", "Mvita", "Mombasa", 30000,
     [("Cassava", 4000, G1, "Cassava flour milling", "Sacks, peeled within 24 h", 130000),
      ("Mango", 1500, G2, "Dried mango slices", "Plastic crates", 60000)]),
    ("Daniel", "Kiprotich", "Rift Valley Unga Mills", "Maize & porridge flour miller", "Nakuru Town East", "Nakuru", 150000,
     [("Maize", 8000, G1, "Sifted maize flour (unga)", "90 kg bags, moisture below 13.5%", 380000),
      ("Beans", 1500, G1, "Fortified porridge flour", "90 kg bags", 190000)]),
    ("Brenda", "Atieno", "Kisumu Crisps & Chips", "Potato crisps & chips", "Kisumu Central", "Kisumu", 25000,
     [("Irish potato", 5000, G1, "Potato crisps (Shangi preferred)", "50 kg nets", 240000),
      ("Sweet potato", 1500, G1, "Sweet potato chips", "Sacks", 70000)]),
    ("Joyce", "Muthoni", "Mama Mboga Fresh Cuts", "Fresh-cut vegetables for supermarkets", "Westlands", "Nairobi", 18000,
     [("Sukuma wiki", 1000, G1, "Washed, chopped sukuma packs", "Bundles in crates", 35000),
      ("Cabbage", 2000, G1, "Coleslaw mix", "Nets", 55000),
      ("Beef", 600, G1, "Ready-to-cook beef stew packs", "Chilled 0-4 °C, vacuum-packed", 360000)]),
    ("Stephen", "Mutiso", "Ukambani Ndengu Packers", "Pulses cleaning & packing", "Machakos Town", "Machakos", 40000,
     [("Green grams", 1500, G1, "Packed ndengu, 1 kg retail", "90 kg bags", 220000),
      ("Beans", 2000, G2, "Packed beans, 1 kg retail", "90 kg bags", 230000)]),
    ("Esther", "Wairimu", "Highland Avocado Oil", "Cold-pressed avocado oil", "Thika Town", "Kiambu", 35000,
     [("Avocado", 2000, G1, "Cold-pressed avocado oil", "Crates, mature fruit", 140000),
      ("Macadamia", 600, G1, "Macadamia oil & roasted nuts", "Sacks, in shell", 120000)]),
    ("Kevin", "Omondi", "Lake Basin Peanut Butter", "Peanut butter & snacks", "Kitutu Chache South", "Kisii", 15000,
     [("Groundnuts", 2000, G1, "Smooth peanut butter", "Sacks, aflatoxin tested", 340000),
      ("Pineapple", 1500, G1, "Pineapple jam", "Crates", 80000)]),
    # A buyer still waiting for verification: no requests until an admin approves them.
    ("Faith", "Chepkoech", "Kerio Valley Honey & Fruit", "Fruit preserves", "Keiyo North", "Elgeyo-Marakwet", 8000, []),
]

# (first, last, business, constituency, county, capacity kg, refrigerated, coverage)
LOGISTICS = [
    ("John", "Kamau", "Mombasa Road Haulers", "Embakasi South", "Nairobi", 10000, True, "Nairobi, Machakos, Makueni, Mombasa"),
    ("Hassan", "Abdi", "Garissa Express Freight", "Garissa Township", "Garissa", 8000, False, "Garissa, Tana River, Nairobi"),
    ("Lucy", "Wangari", "Kati Cold Chain", "Kiambu", "Kiambu", 5000, True, "Kiambu, Murang'a, Nairobi, Nyeri"),
    ("Collins", "Otieno", "Nyanza Fresh Movers", "Kisumu East", "Kisumu", 7000, True, "Kisumu, Homa Bay, Siaya, Kisii"),
    ("Moses", "Kiplagat", "Eldoret Agri Transport", "Kesses", "Uasin Gishu", 12000, False, "Uasin Gishu, Trans Nzoia, Nandi, Bungoma"),
    ("Rose", "Nyambura", "Mlima Logistics", "Nyeri Town", "Nyeri", 4000, False, "Nyeri, Kirinyaga, Murang'a, Laikipia"),
    ("Abdallah", "Omar", "Pwani Reefer Services", "Kisauni", "Mombasa", 9000, True, "Mombasa, Kilifi, Kwale, Taita Taveta"),
    ("Nelson", "Mwenda", "Meru Highway Carriers", "North Imenti", "Meru", 6000, False, "Meru, Embu, Tharaka-Nithi, Isiolo"),
    ("Agnes", "Kerubo", "Gusii Highlands Transport", "Nyaribari Chache", "Kisii", 3000, False, "Kisii, Nyamira, Narok"),
    # Pending verification (shows in the admin queue).
    ("Samuel", "Langat", "Bonde Freight Co.", "Bomet Central", "Bomet", 5000, False, "Bomet, Kericho, Nakuru"),
]

ADMINS = [("Ann", "Mukami"), ("Brian", "Kiptoo"), ("Cynthia", "Awino"), ("David", "Mwangi"), ("Eunice", "Nduta"),
          ("Francis", "Wafula"), ("Gladys", "Jepkosgei"), ("Hussein", "Mohamed"), ("Irene", "Kemunto"),
          ("Joseph", "Odhiambo")]

PENDING_FARMER = ("Wekesa", "Barasa")  # left in the verification queue; others grow the same crops


def username(first, last):
    return f"{first}.{last}".lower().replace("'", "")


class Command(BaseCommand):
    help = "Populate Kenyan demo users, listings with photos, requests, quotations and orders (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--password", help="Password for accounts this run creates (default: random, printed)")
        parser.add_argument("--photos", default=str(settings.PROJECT_ROOT / "demo_assets" / "product_photos"))

    def handle(self, *args, password=None, photos=None, **opts):
        self.photos_dir = Path(photos)
        credits_file = self.photos_dir / "credits.json"
        if not credits_file.exists():
            raise CommandError(f"No product photos at {self.photos_dir} (expected credits.json and *.jpg)")
        self.credits = json.loads(credits_file.read_text())
        self.password = password or secrets.token_urlsafe(12)
        self.created_users = 0
        self.today = timezone.localdate()
        self.now = timezone.now()

        MatchingConfig.get()
        if not Location.objects.filter(name__endswith=" County").exists():
            call_command("load_kenya_locations", stdout=self.stdout)
        with transaction.atomic():
            self._catalogue()
            admins = self._admins()
            farmers = self._farmers()
            buyers = self._processors()
            logistics = self._logistics()
            self._approvals(admins[0], farmers + buyers + logistics)
        self._listings(farmers)
        with transaction.atomic():
            summary = self._requests(buyers)

        self.stdout.write(self.style.SUCCESS("\nKenya demo data loaded."))
        self.stdout.write(f"  Users: {len(admins)} admins, {len(farmers)} farmers, {len(buyers)} processors, "
                          f"{len(logistics)} logistics partners ({self.created_users} created this run)")
        self.stdout.write(f"  Listings: {InventoryItem.objects.filter(quality_attributes__other_details__startswith=DEMO_TAG).count()}, "
                          f"photos: {InventoryPhoto.objects.filter(caption__contains='Wikimedia').count()}")
        self.stdout.write("  Requests: " + ", ".join(f"{k} {v}" for k, v in summary.items()))
        self.stdout.write(f"  Logins: first.last (e.g. {username(*FARMERS[0][:2])}, {username(*PROCESSORS[0][:2])}, "
                          f"{username(*ADMINS[0])})")
        if self.created_users:
            self.stdout.write(self.style.WARNING(f"  Password for the {self.created_users} new account(s): "
                                                 f"{self.password}"))

    # --- reference data ----------------------------------------------------------------
    def _catalogue(self):
        for name, (category, _, varieties, perishable) in CATALOGUE.items():
            cat, _ = Category.objects.get_or_create(name=category)
            product, created = Product.objects.get_or_create(
                name=name, defaults={"category": cat, "perishable": perishable, "quality_parameters": []})
            for v in varieties:
                ProductVariety.objects.get_or_create(product=product, name=v)

    def _location(self, constituency, county):
        loc = (Location.objects.filter(name=constituency, county=county).first()
               or Location.objects.filter(name=f"{county} County").first())
        if loc is None:
            raise CommandError(f"Unknown location {constituency!r} in {county!r}; run load_kenya_locations")
        return loc

    # --- people ----------------------------------------------------------------------
    def _user(self, first, last, role, **extra):
        uname = username(first, last)
        user, created = User.objects.get_or_create(username=uname, defaults={
            "role": role, "first_name": first, "last_name": last, "email": f"{uname}@{EMAIL_DOMAIN}",
            "phone": f"+2547{int(hashlib.sha256(uname.encode()).hexdigest(), 16) % 10**8:08d}", **extra})
        if created:
            user.set_password(self.password)
            user.save()
            self.created_users += 1
        return user

    def _admins(self):
        return [self._user(f, l, Role.ADMIN, is_staff=True) for f, l in ADMINS]

    def _farmers(self):
        out = []
        for first, last, business, const, county, radius, _ in FARMERS:
            user = self._user(first, last, Role.FARMER)
            profile, _ = SupplierProfile.objects.get_or_create(user=user, defaults={
                "business_name": business, "location": self._location(const, county), "can_deliver": True,
                "delivery_radius_km": radius, "farm_size_acres": Decimal(5 + len(business) % 20),
                "certifications": "KEBS, KEPHIS registered" if len(business) % 2 else "",
                "description": f"Family farm in {const}, {county} County.",
                "verification_status": VerificationStatus.PENDING})
            out.append(profile)
        return out

    def _processors(self):
        out = []
        for first, last, business, btype, const, county, volume, _ in PROCESSORS:
            user = self._user(first, last, Role.PROCESSOR)
            profile, _ = BuyerProfile.objects.get_or_create(user=user, defaults={
                "business_name": business, "business_type": btype, "location": self._location(const, county),
                "monthly_volume_kg": volume, "registration_number": f"PVT-{2015 + len(business) % 9}-{volume // 10}",
                "verification_status": VerificationStatus.PENDING})
            out.append(profile)
        return out

    def _logistics(self):
        out = []
        for first, last, business, const, county, capacity, cold, coverage in LOGISTICS:
            user = self._user(first, last, Role.LOGISTICS)
            profile, _ = LogisticsProfile.objects.get_or_create(user=user, defaults={
                "business_name": business, "location": self._location(const, county),
                "vehicle_capacity_kg": capacity, "refrigerated": cold, "coverage_counties": coverage,
                "verification_status": VerificationStatus.PENDING})
            out.append(profile)
        return out

    def _approvals(self, admin, profiles):
        """Admin verification: approve everyone except one per role, left in the review queue."""
        pending = {username(*PENDING_FARMER), username(*PROCESSORS[-1][:2]), username(*LOGISTICS[-1][:2])}
        for p in profiles:
            if p.verification_status == VerificationStatus.PENDING and p.user.username not in pending:
                set_verification(p, admin, VerificationStatus.APPROVED, notes=f"{DEMO_TAG} documents checked")

    # --- listings + photos -------------------------------------------------------------
    def _listings(self, farmers):
        for profile, (*_, stock) in zip(farmers, FARMERS):
            for n, (product_name, variety, kg, grade, price) in enumerate(stock):
                product = Product.objects.get(name=product_name)
                with transaction.atomic():
                    item, _ = InventoryItem.objects.get_or_create(
                        supplier=profile, product=product, variety=ProductVariety.objects.get(product=product, name=variety),
                        defaults={"quantity_available": kg, "unit": "kg", "quality_grade": grade, "price_per_unit": price,
                                  "location": profile.location, "harvest_date": self.today - timedelta(days=3),
                                  "available_from": self.today, "available_until": self.today + timedelta(days=30),
                                  "can_deliver": True, "packaging": "Crates / bags as agreed", "is_active": True,
                                  "quality_attributes": {"other_details": f"{DEMO_TAG} Freshly harvested {variety} "
                                                                          f"{product_name.lower()}, sorted and graded."},
                                  "last_confirmed_at": self.now})
                    InventoryItem.objects.filter(pk=item.pk).update(last_confirmed_at=self.now)  # keep matchable
                self._photos(item, CATALOGUE[product_name][1], start=n)

    def _photos(self, item, slug, start):
        files = sorted(self.photos_dir.glob(f"{slug}-*.jpg"))
        need = max(settings.MIN_PHOTOS_PER_LISTING - item.photos.count(), 0)
        if not files:  # no photo set for this product (e.g. beef): placeholders the supplier replaces
            from apps.core.demo_images import placeholder_png

            palettes = [((150, 40, 40), (90, 30, 25)), ((180, 70, 60), (110, 45, 35))]
            for k in range(need):
                top, bottom = palettes[(start + k) % len(palettes)]
                photo = InventoryPhoto(inventory_item=item, caption="Placeholder photo – replace with your own")
                photo.image.save(f"{slug}-placeholder-{k + 1}.png", ContentFile(placeholder_png(top, bottom, size=480)),
                                 save=False)
                photo.save()
            return
        for k in range(need):
            f = files[(start + k) % len(files)]
            c = self.credits.get(f.name, {})
            caption = f"Photo: {c.get('author', 'Unknown')[:40]}, {c.get('licence', '')} via Wikimedia"[:120]
            photo = InventoryPhoto(inventory_item=item, caption=caption)
            photo.image.save(f.name, ContentFile(f.read_bytes()), save=False)  # storage: Cloudinary on the live site
            photo.save()

    # --- requests, quotations, approvals, orders --------------------------------------
    def _requests(self, buyers):
        summary = {"open": 0, "quoted": 0, "ordered": 0, "existing": 0, "no match": 0}
        flow = ["ordered", "quoted", "open"]
        n = 0
        for profile, (*_, wanted) in zip(buyers, PROCESSORS):
            if profile.verification_status != VerificationStatus.APPROVED:
                continue
            for product_name, kg, grade, use, packaging, budget in wanted:
                stage = flow[n % 3]
                n += 1
                product = Product.objects.get(name=product_name)
                if SourcingRequest.objects.filter(buyer=profile.user, product=product,
                                                  quality_notes__startswith=DEMO_TAG).exists():
                    summary["existing"] += 1
                    continue
                req, result = sourcing.create_sourcing_request(
                    profile.user, product=product, quantity=Decimal(kg), unit="kg",
                    delivery_location=profile.location, delivery_deadline=self.today + timedelta(days=10 + n % 7),
                    min_quality_grade=grade, quality_specifications={},
                    quality_notes=f"{DEMO_TAG} Clean, sorted produce; no pest damage.",
                    intended_application=use, packaging_requirements=packaging, target_budget=Decimal(budget),
                    delivery_arrangement=DeliveryArrangement.SUPPLIER_DELIVERS)
                matches = list(req.matches.select_related("supplier__user", "inventory_item").order_by("rank"))
                if not matches:
                    summary["no match"] += 1
                    continue
                if stage == "open":
                    summary["open"] += 1
                    continue
                sourcing.notify_matched_suppliers(profile.user, req,
                                                  message="Please confirm availability and send your best price.")
                quotes = []
                for m in matches[:3]:
                    try:
                        qty = min(Decimal(m.suggested_quantity or m.available_quantity), Decimal(kg))
                        sourcing.respond_to_match(m.supplier.user, m, available=True, quantity=qty)
                        quotes.append(sourcing.submit_quotation(
                            m.supplier.user, req, m.inventory_item, qty, m.inventory_item.price_per_unit,
                            self.today + timedelta(days=3), self.today + timedelta(days=7),
                            delivery_cost=Decimal(1500 + 500 * m.rank), includes_delivery=True,
                            notes="Can deliver within the week."))
                    except WorkflowError as exc:
                        self.stdout.write(f"    skipped a quote on {req.reference}: {exc}")
                if stage == "quoted" or not quotes:
                    summary["quoted"] += 1
                    continue
                try:
                    best = min(quotes, key=lambda q: q.total)
                    sourcing.accept_quotation(profile.user, best)
                    order = orders.create_order(profile.user, req, [best], notes=f"{DEMO_TAG} first order")
                    for line in order.lines.select_related("supplier__user"):
                        orders.reconfirm_line(line.supplier.user, line, available=True, note="Stock reserved")
                    summary["ordered"] += 1
                except WorkflowError as exc:
                    self.stdout.write(f"    could not place order for {req.reference}: {exc}")
                    summary["quoted"] += 1
        return summary
