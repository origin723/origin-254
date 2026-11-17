from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Location(TimeStampedModel):
    """A named point on the map (farm, collection centre, factory, town).

    Coordinates are WGS84. On PostGIS databases a GiST index over the derived
    geography is created (see migration 0002) and radius searches use
    ST_DWithin; elsewhere a bounding-box + haversine fallback is used.
    """

    name = models.CharField(max_length=160)
    county = models.CharField(max_length=80, db_index=True)
    sub_county = models.CharField(max_length=80, blank=True)
    address = models.CharField(max_length=255, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6,
                                   validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude = models.DecimalField(max_digits=9, decimal_places=6,
                                    validators=[MinValueValidator(-180), MaxValueValidator(180)])

    class Meta:
        ordering = ["county", "name"]

    def __str__(self):
        return f"{self.name}, {self.county}"

    @property
    def coords(self):
        return float(self.latitude), float(self.longitude)


class AuditLog(models.Model):
    """Append-only record of sensitive actions (verification, config, orders)."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="audit_entries")
    action = models.CharField(max_length=80, db_index=True)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    target_repr = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["target_type", "target_id"])]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.target_repr}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Audit log entries are immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit log entries are immutable")


class MatchingConfig(models.Model):
    """Singleton holding the configurable weights of the matching engine."""

    weight_product = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.15"))
    weight_quantity = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.20"))
    weight_quality = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.20"))
    weight_proximity = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.20"))
    weight_delivery = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.10"))
    weight_reliability = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.15"))
    max_distance_km = models.PositiveIntegerField(default=600)
    min_score = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0.350"),
                                    help_text="Matches scoring below this (0-1) are discarded.")
    allow_one_grade_below = models.BooleanField(
        default=False, help_text="Include suppliers one quality grade below the request (penalised).")
    max_matches = models.PositiveIntegerField(default=20)
    new_supplier_reliability = models.DecimalField(
        max_digits=4, decimal_places=3, default=Decimal("0.600"),
        help_text="Reliability assumed for suppliers with no completed transactions.")
    transport_km_per_day = models.PositiveIntegerField(default=400)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        verbose_name = "matching configuration"
        verbose_name_plural = "matching configuration"

    def __str__(self):
        return "Matching configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def weights(self) -> dict:
        raw = {
            "product": self.weight_product,
            "quantity": self.weight_quantity,
            "quality": self.weight_quality,
            "proximity": self.weight_proximity,
            "delivery": self.weight_delivery,
            "reliability": self.weight_reliability,
        }
        total = sum(raw.values()) or Decimal(1)
        return {k: float(v / total) for k, v in raw.items()}


class StoredFile(models.Model):
    """An uploaded file kept in the database (MEDIA_STORAGE=database).

    Used when no working object storage or persistent disk is available. Files are
    only ever streamed through permission-checked views, never served directly.
    """

    name = models.CharField(max_length=255, unique=True)
    content = models.BinaryField()
    size = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name
