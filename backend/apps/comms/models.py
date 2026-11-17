from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class Conversation(TimeStampedModel):
    subject = models.CharField(max_length=200)
    participants = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="conversations")
    sourcing_request = models.ForeignKey("sourcing.SourcingRequest", null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name="conversations")
    order = models.ForeignKey("orders.Order", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="conversations")

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.subject


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="messages_sent")
    body = models.TextField(max_length=4000)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_by = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="messages_read")

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.sender}: {self.body[:40]}"


class Notification(models.Model):
    class Kind(models.TextChoices):
        ACCOUNT = "account", "Account"
        MATCH = "match", "New sourcing opportunity"
        AVAILABILITY = "availability", "Availability response"
        QUOTATION = "quotation", "Quotation"
        ORDER = "order", "Order"
        INSPECTION = "inspection", "Inspection"
        DELIVERY = "delivery", "Delivery"
        MESSAGE = "message", "Message"
        REVIEW = "review", "Review"
        DISPUTE = "dispute", "Dispute"
        SYSTEM = "system", "System"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.SYSTEM)
    title = models.CharField(max_length=160)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=255, blank=True)
    channels_sent = models.JSONField(default=list, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "read_at"])]

    def __str__(self):
        return f"{self.user}: {self.title}"
