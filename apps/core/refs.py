from django.utils import timezone


def assign_reference(instance, prefix, field="reference"):
    """Give a freshly saved row a unique human-friendly id, e.g. SR-2026-000042.

    Derived from the primary key, so it is unique without locking.
    """
    if getattr(instance, field):
        return
    year = (getattr(instance, "created_at", None) or timezone.now()).year
    value = f"{prefix}-{year}-{instance.pk:06d}"
    type(instance).objects.filter(pk=instance.pk).update(**{field: value})
    setattr(instance, field, value)
