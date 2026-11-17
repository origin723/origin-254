from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.catalog.models import GRADE_RANK, InventoryItem, Product, ProductVariety, QualityGrade, Unit, to_base_quantity
from apps.core.models import Location, TimeStampedModel
from apps.core.refs import assign_reference


class DeliveryArrangement(models.TextChoices):
    SUPPLIER_DELIVERS = "supplier_delivers", "Supplier delivers to buyer"
    BUYER_COLLECTS = "buyer_collects", "Buyer collects from supplier"
    PLATFORM_LOGISTICS = "platform_logistics", "Origin 254 logistics partner"


class SourcingRequest(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open — awaiting matches"
        MATCHED = "matched", "Suppliers matched"
        NEGOTIATING = "negotiating", "Receiving quotations"
        SUPPLY_CONFIRMED = "supply_confirmed", "Supply confirmed"
        IN_FULFILMENT = "in_fulfilment", "In fulfilment"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    reference = models.CharField(max_length=24, unique=True, blank=True, editable=False)
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sourcing_requests")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sourcing_requests")
    variety = models.ForeignKey(ProductVariety, null=True, blank=True, on_delete=models.SET_NULL)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.KG)
    delivery_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+",
                                          help_text="Preferred delivery / sourcing location")
    delivery_deadline = models.DateField()
    min_quality_grade = models.CharField(max_length=12, choices=QualityGrade.choices, default=QualityGrade.GRADE_1)
    quality_specifications = models.JSONField(default=dict, blank=True,
                                              help_text='e.g. {"min_size_mm": 70}')
    quality_notes = models.TextField(blank=True)
    intended_application = models.CharField(max_length=160, help_text="e.g. mango jam production")
    packaging_requirements = models.CharField(max_length=200, blank=True)
    target_budget = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
                                        validators=[MinValueValidator(Decimal("1"))],
                                        help_text="Optional total budget in KES")
    delivery_arrangement = models.CharField(max_length=24, choices=DeliveryArrangement.choices,
                                            default=DeliveryArrangement.SUPPLIER_DELIVERS)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    last_matched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} {self.quantity}{self.unit} {self.product}"

    def clean(self):
        errors = {}
        if self.delivery_deadline and self.delivery_deadline < timezone.localdate():
            errors["delivery_deadline"] = "The delivery deadline cannot be in the past."
        if self.variety_id and self.product_id and self.variety.product_id != self.product_id:
            errors["variety"] = "This variety does not belong to the selected product."
        if self.product_id and not self.product.is_active:
            errors["product"] = "This product is not currently available on the platform."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        assign_reference(self, "SR")

    @property
    def base_quantity(self):
        return to_base_quantity(self.quantity, self.unit)

    @property
    def required_grade_rank(self):
        return GRADE_RANK[self.min_quality_grade]

    @property
    def is_active(self):
        return self.status not in {self.Status.COMPLETED, self.Status.CANCELLED}

    def committed_quantity(self):
        from apps.orders.models import OrderLine
        total = (OrderLine.objects.filter(order__sourcing_request=self)
                 .exclude(status__in=OrderLine.INACTIVE_STATUSES)
                 .aggregate(t=models.Sum("quantity"))["t"])
        return total or Decimal(0)

    def accepted_quantity(self):
        total = self.quotations.filter(status=Quotation.Status.ACCEPTED).aggregate(t=models.Sum("quantity"))["t"]
        return total or Decimal(0)


class Match(TimeStampedModel):
    """A supplier inventory listing proposed for a sourcing request.

    Lifecycle distinguishes the three levels the business requires:
    potential match -> confirmed availability -> confirmed supply commitment.
    """

    class Status(models.TextChoices):
        POTENTIAL = "potential", "Potential match"
        CONTACTED = "contacted", "Supplier notified"
        AVAILABILITY_CONFIRMED = "availability_confirmed", "Availability confirmed"
        DECLINED = "declined", "Declined by supplier"
        COMMITTED = "committed", "Supply committed"
        EXPIRED = "expired", "No longer valid"

    sourcing_request = models.ForeignKey(SourcingRequest, on_delete=models.CASCADE, related_name="matches")
    supplier = models.ForeignKey("accounts.SupplierProfile", on_delete=models.CASCADE, related_name="matches")
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="matches")
    rank = models.PositiveIntegerField(default=0)
    score = models.DecimalField(max_digits=5, decimal_places=4)
    score_breakdown = models.JSONField(default=dict)
    distance_km = models.DecimalField(max_digits=8, decimal_places=1)
    available_quantity = models.DecimalField(max_digits=12, decimal_places=2)
    suggested_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                             help_text="Allocation proposed by the multi-supplier planner")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.POTENTIAL, db_index=True)
    confirmed_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    supplier_note = models.TextField(blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sourcing_request", "rank"]
        unique_together = [("sourcing_request", "inventory_item")]
        verbose_name_plural = "matches"

    def __str__(self):
        return f"{self.sourcing_request.reference} ↔ {self.supplier} ({self.score})"


class Quotation(TimeStampedModel):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        COUNTERED = "countered", "Buyer counter-offer"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"
        EXPIRED = "expired", "Expired"

    OPEN_STATUSES = {Status.SUBMITTED, Status.COUNTERED}

    reference = models.CharField(max_length=24, unique=True, blank=True, editable=False)
    sourcing_request = models.ForeignKey(SourcingRequest, on_delete=models.CASCADE, related_name="quotations")
    match = models.ForeignKey(Match, null=True, blank=True, on_delete=models.SET_NULL, related_name="quotations")
    supplier = models.ForeignKey("accounts.SupplierProfile", on_delete=models.PROTECT, related_name="quotations")
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="quotations")
    quantity = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    price_per_unit = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    delivery_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    quality_grade = models.CharField(max_length=12, choices=QualityGrade.choices)
    earliest_delivery_date = models.DateField()
    valid_until = models.DateField()
    includes_delivery = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SUBMITTED, db_index=True)
    revision = models.PositiveIntegerField(default=1)
    counter_price_per_unit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    counter_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    counter_note = models.TextField(blank=True)
    history = models.JSONField(default=list, blank=True, help_text="Negotiation trail")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} {self.supplier} {self.quantity}@{self.price_per_unit}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        assign_reference(self, "QT")

    @property
    def goods_total(self):
        return (self.quantity * self.price_per_unit).quantize(Decimal("0.01"))

    @property
    def total(self):
        return self.goods_total + self.delivery_cost

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES and self.valid_until >= timezone.localdate()

    def record(self, actor, event, **data):
        self.history = [*self.history, {
            "at": timezone.now().isoformat(), "by": actor.username if actor else None, "event": event,
            **{k: str(v) for k, v in data.items()},
        }]
