"""Platform metrics used by the admin dashboard and /api/v1/admin/analytics/."""
from datetime import timedelta

from django.db.models import Avg, Count, DecimalField, ExpressionWrapper, F, Min, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone


def platform_metrics(days=None):
    from apps.accounts.models import BuyerProfile, LogisticsProfile, SupplierProfile, User, VerificationStatus
    from apps.orders.models import Delivery, Dispute, Inspection, Order, OrderLine
    from apps.sourcing.models import Match, Quotation, SourcingRequest

    since = timezone.now() - timedelta(days=days) if days else None
    window = (lambda qs, field="created_at": qs.filter(**{f"{field}__gte": since})) if since else (lambda qs, f=None: qs)

    pending = {
        "suppliers": SupplierProfile.objects.filter(verification_status=VerificationStatus.PENDING).count(),
        "buyers": BuyerProfile.objects.filter(verification_status=VerificationStatus.PENDING).count(),
        "logistics": LogisticsProfile.objects.filter(verification_status=VerificationStatus.PENDING).count(),
    }
    users_by_role = dict(User.objects.values_list("role").annotate(n=Count("id")).order_by())
    approved_suppliers = SupplierProfile.objects.filter(verification_status=VerificationStatus.APPROVED).count()

    requests = window(SourcingRequest.objects.all())
    total_requests = requests.count()
    requests_by_status = dict(requests.values_list("status").annotate(n=Count("id")).order_by())
    matched = requests.annotate(m=Count("matches")).filter(m__gt=0)
    covered = requests.annotate(planned=Sum("matches__suggested_quantity")).filter(planned__gte=F("quantity"))
    with_quotes = requests.annotate(q=Count("quotations")).filter(q__gt=0)
    first_quote = (requests.annotate(first=Min("quotations__created_at")).filter(first__isnull=False)
                   .values_list("created_at", "first"))
    hours = [(b - a).total_seconds() / 3600 for a, b in first_quote]
    matches = Match.objects.filter(sourcing_request__in=requests)
    match_stats = matches.aggregate(avg_score=Avg("score"), avg_distance=Avg("distance_km"), n=Count("id"))
    responded = matches.filter(status__in=[Match.Status.AVAILABILITY_CONFIRMED, Match.Status.COMMITTED,
                                           Match.Status.DECLINED]).count()
    contacted = matches.exclude(status=Match.Status.POTENTIAL).count()

    lines = window(OrderLine.objects.all())
    value_expr = ExpressionWrapper(F("quantity") * F("price_per_unit") + F("delivery_cost"),
                                   output_field=DecimalField(max_digits=16, decimal_places=2))
    completed_lines = lines.filter(status=OrderLine.Status.COMPLETED)
    gmv = completed_lines.aggregate(v=Sum(value_expr))["v"] or 0
    delivered_qty = completed_lines.aggregate(q=Sum("delivered_quantity"))["q"] or 0
    deliveries = Delivery.objects.filter(order_line__in=completed_lines, delivered_at__isnull=False)
    on_time = sum(1 for d in deliveries.select_related("order_line")
                  if timezone.localtime(d.delivered_at).date() <= d.order_line.agreed_delivery_date)
    inspections = window(Inspection.objects.all())
    n_insp = inspections.count()

    orders = window(Order.objects.all())
    multi_supplier = orders.annotate(n=Count("lines", filter=~Q(lines__status=OrderLine.Status.CANCELLED))) \
        .filter(n__gt=1).count()

    top_products = list(requests.values("product__name").annotate(requests=Count("id"), quantity=Sum("quantity"))
                        .order_by("-requests")[:5])
    top_suppliers = list(completed_lines.values("supplier__business_name")
                         .annotate(orders=Count("id"), delivered=Sum("delivered_quantity"), value=Sum(value_expr))
                         .order_by("-value")[:5])
    six_months = timezone.now() - timedelta(days=183)
    trend_requests = dict(SourcingRequest.objects.filter(created_at__gte=six_months)
                          .annotate(m=TruncMonth("created_at")).values_list("m").annotate(n=Count("id")).order_by())
    trend_orders = dict(Order.objects.filter(created_at__gte=six_months)
                        .annotate(m=TruncMonth("created_at")).values_list("m").annotate(n=Count("id")).order_by())
    months = sorted(set(trend_requests) | set(trend_orders))

    ratio = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    return {
        "generated_at": timezone.now(),
        "window_days": days,
        "users": {"by_role": users_by_role, "pending_verification": pending, "approved_suppliers": approved_suppliers},
        "sourcing": {
            "total_requests": total_requests,
            "by_status": requests_by_status,
            "match_rate": ratio(matched.count(), total_requests),
            "full_coverage_rate": ratio(covered.count(), total_requests),
            "quotation_rate": ratio(with_quotes.count(), total_requests),
            "avg_hours_to_first_quote": round(sum(hours) / len(hours), 1) if hours else None,
        },
        "matching": {
            "total_matches": match_stats["n"],
            "avg_matches_per_request": ratio(match_stats["n"], total_requests),
            "avg_score": round(float(match_stats["avg_score"]), 3) if match_stats["avg_score"] else None,
            "avg_distance_km": round(float(match_stats["avg_distance"]), 1) if match_stats["avg_distance"] else None,
            "supplier_response_rate": ratio(responded, contacted),
            "quote_conversion": ratio(Quotation.objects.filter(sourcing_request__in=requests).count(),
                                      match_stats["n"]),
        },
        "orders": {
            "total": orders.count(),
            "by_status": dict(orders.values_list("status").annotate(n=Count("id")).order_by()),
            "multi_supplier_orders": multi_supplier,
            "completed_value_kes": gmv,
            "delivered_quantity": delivered_qty,
            "on_time_delivery_rate": ratio(on_time, deliveries.count()),
            "inspection_pass_rate": ratio(inspections.exclude(result=Inspection.Result.FAILED).count(), n_insp),
            "open_disputes": Dispute.objects.filter(status__in=[Dispute.Status.OPEN,
                                                                Dispute.Status.UNDER_REVIEW]).count(),
        },
        "top_products": top_products,
        "top_suppliers": top_suppliers,
        "monthly_trend": [{"month": m.strftime("%Y-%m"), "requests": trend_requests.get(m, 0),
                           "orders": trend_orders.get(m, 0)} for m in months],
    }
