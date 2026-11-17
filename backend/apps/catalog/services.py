"""Turn product / variety names typed by suppliers into catalogue entries.

Suppliers and buyers type names freely. They are matched to the existing catalogue
case-insensitively, ignoring extra spaces and simple plurals ("mangoes" ->
Mango, "Tomatoes" -> Tomato), so buyer requests still match their stock.
Unknown products are added under "Other (supplier-added)" and administrators
are notified so they can review and categorise them.
"""
import re

from apps.comms.models import Notification
from apps.comms.notify import notify_admins
from apps.core.audit import log_action

from .models import Category, Product, ProductVariety

NEW_PRODUCT_CATEGORY = "Other (supplier-added)"


def clean_name(value):
    value = re.sub(r"\s+", " ", (value or "").strip())
    return value[:1].upper() + value[1:]


def _name_variants(name):
    low = name.lower()
    variants = [low]
    if low.endswith("ies"):
        variants.append(low[:-3] + "y")
    if low.endswith("es"):
        variants.append(low[:-2])
    if low.endswith("s"):
        variants.append(low[:-1])
    variants += [low + "s", low + "es"]
    return variants


def find_product(name):
    name = clean_name(name)
    for variant in _name_variants(name):
        product = Product.objects.filter(name__iexact=variant).first()
        if product:
            return product
    return None


def resolve_product(name, actor=None):
    """Existing product matching `name`, or a new one awaiting admin review. Returns (product, created)."""
    name = clean_name(name)
    product = find_product(name)
    if product:
        return product, False
    category, _ = Category.objects.get_or_create(
        name=NEW_PRODUCT_CATEGORY, defaults={"description": "Products typed in by suppliers or buyers; review and recategorise."})
    product = Product.objects.create(name=name, category=category)
    who = "a buyer" if actor is not None and getattr(actor, "is_buyer", False) else "a supplier"
    log_action(actor, "catalog.product_added_by_buyer" if who == "a buyer" else "catalog.product_added_by_supplier",
               product)
    notify_admins(f"New product added by {who}: {name}",
                  "Review it in the catalogue and move it to the right category.", Notification.Kind.SYSTEM,
                  f"/django-admin/catalog/product/{product.pk}/change/")
    return product, True


def resolve_variety(product, name):
    name = clean_name(name)
    if not name:
        return None
    variety = ProductVariety.objects.filter(product=product, name__iexact=name).first()
    return variety or ProductVariety.objects.create(product=product, name=name)


def add_photos(user, item, files, caption=""):
    """Attach photos to a supplier's listing (validated, capped per listing)."""
    from django.core.exceptions import ValidationError

    from .models import MAX_PHOTOS_PER_LISTING, InventoryPhoto

    if item.supplier.user_id != user.pk:
        raise ValidationError("You can only add photos to your own stock.")
    room = MAX_PHOTOS_PER_LISTING - item.photos.count()
    if len(files) > room:
        raise ValidationError(f"Each listing can have up to {MAX_PHOTOS_PER_LISTING} photos "
                              f"({max(room, 0)} more allowed).")
    from django.db import transaction

    photos = []
    with transaction.atomic():  # all photos or none, e.g. if storage fails part-way
        for f in files:
            photo = InventoryPhoto(inventory_item=item, image=f, caption=caption[:120])
            photo.full_clean()
            photo.save()
            photos.append(photo)
    return photos


def can_view_photo(user, photo):
    """Who may see a product photo.

    - the supplier who owns the listing, and administrators
    - any buyer, for listings buyers can currently find (active stock from an approved
      supplier), since photos are how buyers judge produce in search results
    - buyers who were matched to, quoted on or bought from the listing, even after it is
      sold out or hidden (history)
    - the logistics partner carrying that produce
    """
    from apps.accounts.models import VerificationStatus
    item = photo.inventory_item
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_platform_admin or item.supplier.user_id == user.pk:
        return True
    if user.is_buyer:
        if item.is_active and item.supplier.verification_status == VerificationStatus.APPROVED:
            return True
        return (item.matches.filter(sourcing_request__buyer=user).exists()
                or item.quotations.filter(sourcing_request__buyer=user).exists())
    if user.is_logistics:
        return item.order_lines.filter(delivery__logistics_partner=user).exists()
    return False
