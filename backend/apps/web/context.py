def unread_counts(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    return {"unread_notifications": user.notifications.filter(read_at__isnull=True).count()}


def product_options(request):
    """Names for the shared <datalist>s, so product fields accept typing or picking. Lazy: only
    queried on pages that render the lists."""
    from django.utils.functional import SimpleLazyObject

    from apps.catalog.models import Product, ProductVariety

    return {
        "product_options": SimpleLazyObject(
            lambda: list(Product.objects.filter(is_active=True).order_by("name").values_list("name", flat=True))),
        "variety_options": SimpleLazyObject(
            lambda: list(ProductVariety.objects.order_by("name").values_list("name", flat=True).distinct())),
    }

