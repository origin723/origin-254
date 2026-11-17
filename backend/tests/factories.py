"""Small helpers to build test data on top of the seed_demo dataset."""
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.catalog.models import Product, QualityGrade
from apps.core.models import Location
from apps.sourcing import services
from apps.sourcing.models import DeliveryArrangement

PASSWORD = "Origin254!demo"


def seed():
    call_command("seed_demo", verbosity=0, stdout=open("/dev/null", "w"))


def user(username):
    return User.objects.get(username=username)


def api(username=None):
    client = APIClient()
    if username:
        client.force_authenticate(user(username))
    return client


def mango_request(buyer=None, **overrides):
    data = dict(
        product=Product.objects.get(name="Mango"), quantity=Decimal("1500"), unit="kg",
        delivery_location=Location.objects.get(name="Nairobi Industrial Area"),
        delivery_deadline=timezone.localdate() + timedelta(days=7), min_quality_grade=QualityGrade.GRADE_1,
        intended_application="Mango jam production",
        packaging_requirements="Crates", delivery_arrangement=DeliveryArrangement.SUPPLIER_DELIVERS,
    )
    data.update(overrides)
    return services.create_sourcing_request(buyer or user("mama_jam"), **data)


def quote(req, username, qty, price, delivery=0):
    supplier = user(username)
    match = req.matches.get(supplier__user=supplier)
    today = timezone.localdate()
    return services.submit_quotation(supplier, req, match.inventory_item, Decimal(qty), Decimal(price),
                                     today + timedelta(days=2), today + timedelta(days=5),
                                     delivery_cost=Decimal(delivery))
