import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.catalog.models import InventoryItem, QualityGrade, Unit
from apps.core.models import Location, TimeStampedModel
from apps.core.refs import assign_reference


def _private_path(prefix, filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    return f"{prefix}/{timezone.now():%Y/%m}/{uuid.uuid4().hex}.{ext}"


def upload_inspection(instance, filename):
    return _private_path("inspections", filename)


def upload_pod(instance, filename):
    return _private_path("deliveries", filename)


doc_validators = [FileExtensionValidator(settings.ALLOWED_DOCUMENT_EXTENSIONS)]


class Order(TimeStampedModel):
    """A procurement against one sourcing request; may span several suppliers."""

    class Status(models.TextChoices):
        PENDING_CONFIRMATION = "pending_confirmation", "Awaiting supplier reconfirmation"
        CONFIRMED = "confirmed", "Supply confirmed"
        IN_PROGRESS = "in_progress", "Inspection / delivery in progress"
        DELIVERED = "delivered", "Delivered"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        DISPUTED = "disputed", "Disputed"

    reference = models.CharField(max_length=24, unique=True, blank=True, editable=False)
    sourcing_request = models.ForeignKey("sourcing.SourcingRequest", on_delete=models.PROTECT, related_name="orders")
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING_CONFIRMATION,
                              db_index=True)
    delivery_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+")
    notes = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.reference or f"Order {self.pk}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        assign_reference(self, "PO")

    @property
    def total(self):
        return sum((line.total for line in self.lines.exclude(status__in=OrderLine.INACTIVE_STATUSES)),
                   Decimal(0))

    @property
    def total_quantity(self):
        return sum((line.quantity for line in self.lines.exclude(status__in=OrderLine.INACTIVE_STATUSES)), 0)


class OrderLine(TimeStampedModel):
    """The confirmed supply agreement with one supplier."""

    class Status(models.TextChoices):
        AWAITING_RECONFIRMATION = "awaiting_reconfirmation", "Awaiting supplier reconfirmation"
        CONFIRMED = "confirmed", "Supply confirmed"
        INSPECTION_PASSED = "inspection_passed", "Passed inspection"
        INSPECTION_FAILED = "inspection_failed", "Failed inspection"
        IN_TRANSIT = "in_transit", "In transit"
        DELIVERED = "delivered", "Delivered"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    INACTIVE_STATUSES = {Status.CANCELLED, Status.INSPECTION_FAILED}

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    supplier = models.ForeignKey("accounts.SupplierProfile", on_delete=models.PROTECT, related_name="order_lines")
    quotation = models.OneToOneField("sourcing.Quotation", on_delete=models.PROTECT, related_name="order_line")
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.PROTECT, related_name="order_lines")
    quantity = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    unit = models.CharField(max_length=10, choices=Unit.choices)
    price_per_unit = models.DecimalField(max_digits=12, decimal_places=2)
    delivery_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    quality_grade = models.CharField(max_length=12, choices=QualityGrade.choices)
    quality_specifications = models.JSONField(default=dict, blank=True,
                                              help_text="Snapshot of the agreed quality requirements (traceability)")
    agreed_delivery_date = models.DateField()
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.AWAITING_RECONFIRMATION,
                              db_index=True)
    reconfirmed_at = models.DateTimeField(null=True, blank=True)
    delivered_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.order.reference} · {self.supplier} · {self.quantity}{self.unit}"

    @property
    def total(self):
        return (self.quantity * self.price_per_unit + self.delivery_cost).quantize(Decimal("0.01"))

    @property
    def delivered_on_time(self):
        delivery = getattr(self, "delivery", None)
        if not delivery or not delivery.delivered_at:
            return None
        return timezone.localtime(delivery.delivered_at).date() <= self.agreed_delivery_date


class Inspection(TimeStampedModel):
    class Stage(models.TextChoices):
        PRE_DISPATCH = "pre_dispatch", "Pre-dispatch (at source)"
        ON_RECEIPT = "on_receipt", "On receipt (at buyer)"

    class Result(models.TextChoices):
        PASSED = "passed", "Passed"
        CONDITIONAL = "conditional", "Passed with conditions"
        FAILED = "failed", "Failed"

    order_line = models.ForeignKey(OrderLine, on_delete=models.CASCADE, related_name="inspections")
    stage = models.CharField(max_length=14, choices=Stage.choices, default=Stage.PRE_DISPATCH)
    inspector = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="inspections")
    inspected_at = models.DateTimeField(default=timezone.now)
    result = models.CharField(max_length=12, choices=Result.choices)
    grade_observed = models.CharField(max_length=12, choices=QualityGrade.choices, blank=True)
    measurements = models.JSONField(default=dict, blank=True, help_text='e.g. {"moisture_pct": 13}')
    accepted_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    rejected_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)
    report = models.FileField(upload_to=upload_inspection, null=True, blank=True, validators=doc_validators)

    class Meta:
        ordering = ["-inspected_at"]

    def __str__(self):
        return f"{self.get_stage_display()} {self.result} — {self.order_line}"


class Delivery(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting scheduling"
        SCHEDULED = "scheduled", "Scheduled"
        PICKED_UP = "picked_up", "Picked up"
        IN_TRANSIT = "in_transit", "In transit"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"

    order_line = models.OneToOneField(OrderLine, on_delete=models.CASCADE, related_name="delivery")
    logistics_partner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name="deliveries")
    pickup_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+")
    dropoff_location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    scheduled_date = models.DateField(null=True, blank=True)
    vehicle_reg = models.CharField(max_length=20, blank=True)
    driver_name = models.CharField(max_length=80, blank=True)
    driver_phone = models.CharField(max_length=16, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    received_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    received_by = models.CharField(max_length=120, blank=True)
    proof_of_delivery = models.FileField(upload_to=upload_pod, null=True, blank=True, validators=doc_validators)
    tracking_log = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "deliveries"

    def __str__(self):
        return f"Delivery {self.order_line} ({self.status})"

    def log(self, actor, status, note=""):
        self.tracking_log = [*self.tracking_log, {
            "at": timezone.now().isoformat(), "status": status,
            "by": actor.username if actor else None, "note": note,
        }]


class OrderEvent(models.Model):
    """Timeline of everything that happens on an order (documentation trail)."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")
    order_line = models.ForeignKey(OrderLine, null=True, blank=True, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    event = models.CharField(max_length=60)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.order} {self.event}"


class Dispute(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        UNDER_REVIEW = "under_review", "Under review"
        RESOLVED = "resolved", "Resolved"
        REJECTED = "rejected", "Rejected"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="disputes")
    order_line = models.ForeignKey(OrderLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="disputes")
    raised_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="disputes_raised")
    reason = models.TextField()
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN, db_index=True)
    resolution = models.TextField(blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="disputes_resolved")
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Dispute on {self.order} ({self.status})"


class SaleRecord(TimeStampedModel):
    """Permanent record of a completed purchase: what was bought, from whom, by whom, at what price.

    Written once when the buyer confirms receipt. Key details are copied (not just linked) so the
    history stays accurate even if the listing is later edited, sold out or removed.
    """

    order_line = models.OneToOneField(OrderLine, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="sale_record")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="sale_records")
    order_reference = models.CharField(max_length=24)
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="purchase_history")
    buyer_name = models.CharField(max_length=160)
    supplier = models.ForeignKey("accounts.SupplierProfile", on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="sales_history")
    supplier_name = models.CharField(max_length=160)
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="sale_records")
    product_name = models.CharField(max_length=120)
    variety_name = models.CharField(max_length=80, blank=True)
    quality_grade = models.CharField(max_length=12, choices=QualityGrade.choices)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    unit = models.CharField(max_length=10, choices=Unit.choices)
    price_per_unit = models.DecimalField(max_digits=12, decimal_places=2)
    delivery_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    delivered_to = models.CharField(max_length=200, blank=True)
    completed_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-completed_at", "-id"]

    def __str__(self):
        return f"{self.order_reference} · {self.product_name} {self.quantity:g}{self.unit} · {self.supplier_name}"
