from decimal import Decimal

from django.db import transaction
from django.db.models import Avg

from apps.comms.models import Notification
from apps.comms.notify import notify
from apps.core.exceptions import WorkflowError
from apps.core.models import MatchingConfig

from .models import Review

PRIOR_WEIGHT = 3  # completed transactions needed before the track record dominates


@transaction.atomic
def create_review(user, line, rating, comment="", quality_rating=None, timeliness_rating=None,
                  communication_rating=None):
    """Ratings are only allowed on completed transactions, once per party."""
    from apps.orders.models import OrderLine

    if line.status != OrderLine.Status.COMPLETED:
        raise WorkflowError("You can only review a completed transaction.", "not_completed")
    if line.order.buyer_id == user.pk:
        reviewee = line.supplier.user
    elif line.supplier.user_id == user.pk:
        reviewee = line.order.buyer
    else:
        raise WorkflowError("Only the buyer and supplier of this transaction can review it.", "permission_denied")
    if Review.objects.filter(order_line=line, reviewer=user).exists():
        raise WorkflowError("You have already reviewed this transaction.", "duplicate_review")
    review = Review(order_line=line, reviewer=user, reviewee=reviewee, rating=rating, comment=comment,
                    quality_rating=quality_rating, timeliness_rating=timeliness_rating,
                    communication_rating=communication_rating)
    review.full_clean()
    review.save()
    notify(reviewee, f"You received a {rating}★ review", comment[:200], Notification.Kind.REVIEW)
    if reviewee.is_supplier:
        recompute_supplier_metrics(reviewee.supplier_profile)
    return review


def recompute_supplier_metrics(supplier):
    """Reliability = blend of ratings, on-time delivery and inspection pass rate,
    shrunk towards the new-supplier default until there is enough history."""
    from apps.orders.models import Inspection, OrderLine

    default = float(MatchingConfig.get().new_supplier_reliability)
    completed = list(OrderLine.objects.filter(supplier=supplier, status=OrderLine.Status.COMPLETED)
                     .select_related("delivery"))
    n = len(completed)
    avg_rating = Review.objects.filter(reviewee=supplier.user).aggregate(a=Avg("rating"))["a"]
    on_time_flags = [line.delivered_on_time for line in completed if line.delivered_on_time is not None]
    on_time = sum(on_time_flags) / len(on_time_flags) if on_time_flags else None
    inspections = Inspection.objects.filter(order_line__supplier=supplier)
    total_insp = inspections.count()
    pass_rate = (inspections.exclude(result=Inspection.Result.FAILED).count() / total_insp) if total_insp else None

    parts = [(0.4, avg_rating / 5 if avg_rating else None), (0.3, on_time), (0.3, pass_rate)]
    raw = sum(w * (v if v is not None else default) for w, v in parts)
    reliability = (n * raw + PRIOR_WEIGHT * default) / (n + PRIOR_WEIGHT) if n else None

    supplier.completed_orders = n
    supplier.average_rating = Decimal(str(round(avg_rating, 2))) if avg_rating else None
    supplier.on_time_rate = Decimal(str(round(on_time, 3))) if on_time is not None else None
    supplier.inspection_pass_rate = Decimal(str(round(pass_rate, 3))) if pass_rate is not None else None
    supplier.reliability_score = Decimal(str(round(reliability, 3))) if reliability is not None else None
    supplier.save(update_fields=["completed_orders", "average_rating", "on_time_rate", "inspection_pass_rate",
                                 "reliability_score", "updated_at"])
    return supplier
