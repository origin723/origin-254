"""Buyer payments (M-Pesa STK Push) and supplier payouts.

Money flow: the buyer pays the order total to the Origin 254 paybill. The platform
holds it until the buyer confirms receipt of each supplier's lot, then releases a
payout to that supplier (M-Pesa B2C or recorded manually by an administrator).
"""
from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel
from apps.core.refs import assign_reference


class Payment(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for the buyer to confirm on their phone"
        SUCCEEDED = "succeeded", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled by the buyer"
        REFUNDED = "refunded", "Refunded"

    class Method(models.TextChoices):
        MPESA = "mpesa", "M-Pesa"

    reference = models.CharField(max_length=24, unique=True, blank=True, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="payments")
    payer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="KES")
    method = models.CharField(max_length=12, choices=Method.choices, default=Method.MPESA)
    provider = models.CharField(max_length=20, help_text="simulated or mpesa")
    phone = models.CharField(max_length=16)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    checkout_request_id = models.CharField(max_length=80, blank=True, db_index=True)
    merchant_request_id = models.CharField(max_length=80, blank=True)
    receipt_number = models.CharField(max_length=40, blank=True, help_text="M-Pesa transaction code")
    result_code = models.CharField(max_length=10, blank=True)
    result_description = models.CharField(max_length=255, blank=True)
    raw_callback = models.JSONField(default=dict, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["receipt_number"], condition=~models.Q(receipt_number=""),
                                               name="unique_mpesa_receipt")]

    def __str__(self):
        return f"{self.reference} KES {self.amount:,} {self.get_status_display()}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        assign_reference(self, "PAY")


class Payout(TimeStampedModel):
    class Status(models.TextChoices):
        ON_HOLD = "on_hold", "On hold (dispute open)"
        READY = "ready", "Ready to pay"
        PROCESSING = "processing", "Sending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    class Method(models.TextChoices):
        MPESA_B2C = "mpesa_b2c", "M-Pesa (automatic)"
        MANUAL = "manual", "Recorded manually"
        SIMULATED = "simulated", "Simulated (development)"

    reference = models.CharField(max_length=24, unique=True, blank=True, editable=False)
    order_line = models.OneToOneField("orders.OrderLine", on_delete=models.PROTECT, related_name="payout")
    supplier = models.ForeignKey("accounts.SupplierProfile", on_delete=models.PROTECT, related_name="payouts")
    gross_amount = models.DecimalField(max_digits=14, decimal_places=2)
    fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=14, decimal_places=2, help_text="Paid to the supplier (gross - fee)")
    phone = models.CharField(max_length=16, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.READY, db_index=True)
    method = models.CharField(max_length=12, choices=Method.choices, blank=True)
    conversation_id = models.CharField(max_length=80, blank=True, db_index=True)
    receipt_number = models.CharField(max_length=40, blank=True)
    result_description = models.CharField(max_length=255, blank=True)
    raw_result = models.JSONField(default=dict, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} KES {self.amount:,} to {self.supplier} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        assign_reference(self, "PAYOUT")
