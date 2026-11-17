"""Load all 47 Kenyan counties and 290 constituencies as Locations (idempotent).

    python manage.py load_kenya_locations

Each county gets a "<County> County" location at its centroid, and each
constituency a location at its centroid with sub_county set to the
constituency name. Data: apps/core/data/kenya_locations.json.
"""
import json
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.models import Location

DATA = Path(__file__).resolve().parents[2] / "data" / "kenya_locations.json"


class Command(BaseCommand):
    help = "Load Kenya's 47 counties and 290 constituencies into Locations."

    @transaction.atomic
    def handle(self, *args, **opts):
        data = json.loads(DATA.read_text())
        created = updated = 0
        for county in data["counties"]:
            entries = [(f"{county['name']} County", "", county["lat"], county["lng"])]
            entries += [(c["name"], c["name"], c["lat"], c["lng"]) for c in county["constituencies"]]
            for name, sub_county, lat, lng in entries:
                _, was_created = Location.objects.update_or_create(
                    name=name, county=county["name"],
                    defaults={"sub_county": sub_county, "latitude": Decimal(str(lat)),
                              "longitude": Decimal(str(lng))})
                created += was_created
                updated += not was_created
        n_const = sum(len(c["constituencies"]) for c in data["counties"])
        self.stdout.write(self.style.SUCCESS(
            f"{len(data['counties'])} counties and {n_const} constituencies loaded "
            f"({created} created, {updated} updated)."))
