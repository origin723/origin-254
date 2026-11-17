import uuid
from datetime import timedelta

from django.conf import settings
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.core.models import Location, TimeStampedModel


class Unit(models.TextChoices):
    KG = "kg", "Kilograms"
    TONNE = "tonne", "Tonnes"
    CRATE = "crate", "Crates"
    BAG_90KG = "bag_90kg", "90 kg bags"
    LITRE = "litre", "Litres"
    PIECE = "piece", "Pieces"


# Conversion to kilograms where meaningful (used for quantity comparison).
UNIT_TO_KG = {Unit.KG: 1, Unit.TONNE: 1000, Unit.BAG_90KG: 90, Unit.LITRE: 1}


def to_base_quantity(quantity, unit):
    factor = UNIT_TO_KG.get(unit)
    return float(quantity) * factor if factor else float(quantity)


def units_compatible(a, b):
    """Same unit, or both convertible to kilograms. Crates and pieces vary in size, so only match themselves."""
    return a == b or (a in UNIT_TO_KG and b in UNIT_TO_KG)


def from_base_quantity(base_quantity, unit):
    factor = UNIT_TO_KG.get(unit)
    return float(base_quantity) / factor if factor else float(base_quantity)


class QualityGrade(models.TextChoices):
    PREMIUM = "premium", "Premium / export grade"
    GRADE_1 = "grade_1", "Grade 1"
    GRADE_2 = "grade_2", "Grade 2"
    PROCESSING = "processing", "Processing grade"


GRADE_RANK = {QualityGrade.PREMIUM: 4, QualityGrade.GRADE_1: 3, QualityGrade.GRADE_2: 2, QualityGrade.PROCESSING: 1}


class Category(TimeStampedModel):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=80, unique=True, blank=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.slug = self.slug or slugify(self.name)
        super().save(*args, **kwargs)


class Product(TimeStampedModel):
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    default_unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.KG)
    perishable = models.BooleanField(default=True)
    shelf_life_days = models.PositiveIntegerField(null=True, blank=True)
    quality_parameters = models.JSONField(
        default=list, blank=True,
        help_text='Measurable parameters inspectors record, e.g. ["size_mm", "moisture_pct"].')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.slug = self.slug or slugify(self.name)
        super().save(*args, **kwargs)


class ProductVariety(TimeStampedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="varieties")
    name = models.CharField(max_length=80)

    class Meta:
        ordering = ["product__name", "name"]
        unique_together = [("product", "name")]
        verbose_name_plural = "product varieties"

    def __str__(self):
        return f"{self.product.name} — {self.name}"


class InventoryQuerySet(models.QuerySet):
    def fresh(self):
        cutoff = timezone.now() - timedelta(days=settings.INVENTORY_STALE_DAYS)
        return self.filter(last_confirmed_at__gte=cutoff)

    def with_required_photos(self):
        """Only listings with at least MIN_PHOTOS_PER_LISTING photos are shown to buyers."""
        return (self.annotate(_photo_count=models.Count("photos", distinct=True))
                .filter(_photo_count__gte=settings.MIN_PHOTOS_PER_LISTING))

    def available(self):
        today = timezone.localdate()
        return (self.filter(is_active=True, quantity_available__gt=0)
                .filter(models.Q(available_until__isnull=True) | models.Q(available_until__gte=today)))


class InventoryItem(TimeStampedModel):
    """A supplier's listing of produce they can supply."""

    supplier = models.ForeignKey("accounts.SupplierProfile", on_delete=models.CASCADE, related_name="inventory")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="inventory")
    variety = models.ForeignKey(ProductVariety, null=True, blank=True, on_delete=models.SET_NULL)
    quantity_available = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.KG)
    quality_grade = models.CharField(max_length=12, choices=QualityGrade.choices, default=QualityGrade.GRADE_1)
    quality_attributes = models.JSONField(default=dict, blank=True,
                                          help_text='Declared quality, e.g. {"other_details": "Hand-picked, graded by size"}')
    price_per_unit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                         help_text="Indicative price in KES")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+")
    harvest_date = models.DateField(null=True, blank=True)
    available_from = models.DateField(default=timezone.localdate)
    available_until = models.DateField(null=True, blank=True)
    can_deliver = models.BooleanField(default=False)
    packaging = models.CharField(max_length=120, blank=True, help_text="e.g. crates, sacks, bulk")
    is_active = models.BooleanField(default=True)
    last_confirmed_at = models.DateTimeField(default=timezone.now,
                                             help_text="When the supplier last confirmed this stock is accurate.")

    objects = InventoryQuerySet.as_manager()

    class Meta:
        ordering = ["-last_confirmed_at"]
        indexes = [models.Index(fields=["product", "is_active"])]

    def __str__(self):
        return f"{self.product} {self.quantity_available}{self.unit} — {self.supplier}"

    @property
    def is_stale(self):
        return self.last_confirmed_at < timezone.now() - timedelta(days=settings.INVENTORY_STALE_DAYS)

    @property
    def base_quantity(self):
        return to_base_quantity(self.quantity_available, self.unit)

    @property
    def has_required_photos(self):
        return self.photos.count() >= settings.MIN_PHOTOS_PER_LISTING

    @property
    def photos_missing(self):
        return max(settings.MIN_PHOTOS_PER_LISTING - self.photos.count(), 0)


MAX_PHOTOS_PER_LISTING = 6
MAX_PHOTO_MB = 5
PHOTO_EXTENSIONS = ["jpg", "jpeg", "png", "webp"]


def photo_upload_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    return f"product_photos/{instance.inventory_item.supplier_id}/{uuid.uuid4().hex}.{ext}"


def validate_image_file(f):
    """Check size and the file's real type (magic bytes), not just its extension."""
    from django.core.exceptions import ValidationError
    if f.size > MAX_PHOTO_MB * 1024 * 1024:
        raise ValidationError(f"Photos must be under {MAX_PHOTO_MB} MB.")
    head = f.read(12)
    f.seek(0)
    is_jpeg = head[:3] == b"\xff\xd8\xff"
    is_png = head[:8] == b"\x89PNG\r\n\x1a\n"
    is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if not (is_jpeg or is_png or is_webp):
        raise ValidationError("Upload a JPG, PNG or WebP photo.")


class InventoryPhoto(TimeStampedModel):
    """Pictures of the produce on a stock listing, shown to matched buyers."""

    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="photos")
    image = models.FileField(upload_to=photo_upload_path,
                             validators=[FileExtensionValidator(PHOTO_EXTENSIONS), validate_image_file])
    caption = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Photo of {self.inventory_item}"
