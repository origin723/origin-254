"""Geographic helpers.

Uses PostGIS (ST_DWithin on a geography index) when the extension is
installed in the database, otherwise falls back to a bounding-box prefilter
plus haversine distance in Python. Both give identical results; PostGIS is
faster at scale.
"""
import math
from functools import lru_cache

from django.db import connection

EARTH_RADIUS_KM = 6371.0088


def haversine_km(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


@lru_cache(maxsize=1)
def postgis_enabled() -> bool:
    if connection.vendor != "postgresql":
        return False
    with connection.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")
        return cur.fetchone() is not None


def location_ids_within(lat, lng, radius_km):
    """Return the ids of core.Location rows within `radius_km` of a point."""
    from .models import Location

    if postgis_enabled():
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM core_location
                WHERE ST_DWithin(
                    ST_SetSRID(ST_MakePoint(longitude::float8, latitude::float8), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                    %s)
                """,
                [float(lng), float(lat), float(radius_km) * 1000],
            )
            return [row[0] for row in cur.fetchall()]

    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(math.cos(math.radians(float(lat))), 0.01))
    candidates = Location.objects.filter(
        latitude__gte=float(lat) - dlat, latitude__lte=float(lat) + dlat,
        longitude__gte=float(lng) - dlng, longitude__lte=float(lng) + dlng,
    ).values_list("id", "latitude", "longitude")
    return [pk for pk, la, lo in candidates
            if haversine_km((float(lat), float(lng)), (float(la), float(lo))) <= radius_km]
