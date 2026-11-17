from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.models import TimeStampedModel

rating_validators = [MinValueValidator(1), MaxValueValidator(5)]


class Review(TimeStampedModel):
    """A rating tied to one completed supply agreement (order line)."""

    order_line = models.ForeignKey("orders.OrderLine", on_delete=models.CASCADE, related_name="reviews")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reviews_given")
    reviewee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reviews_received")
    rating = models.PositiveSmallIntegerField(validators=rating_validators)
    quality_rating = models.PositiveSmallIntegerField(null=True, blank=True, validators=rating_validators)
    timeliness_rating = models.PositiveSmallIntegerField(null=True, blank=True, validators=rating_validators)
    communication_rating = models.PositiveSmallIntegerField(null=True, blank=True, validators=rating_validators)
    comment = models.TextField(blank=True, max_length=2000)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("order_line", "reviewer")]

    def __str__(self):
        return f"{self.rating}★ {self.reviewer} → {self.reviewee}"
