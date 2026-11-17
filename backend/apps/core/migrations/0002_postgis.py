"""Enable PostGIS (when installed on the server) and index locations spatially.

On plain PostgreSQL or SQLite this migration is a no-op and the application
falls back to haversine distance (see apps/core/geo.py).
"""
from django.db import migrations


def enable_postgis(apps, schema_editor):
    conn = schema_editor.connection
    if conn.vendor != "postgresql":
        return
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'postgis'")
        if cur.fetchone() is None:
            return
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS core_location_geog_gist ON core_location USING GIST "
            "((ST_SetSRID(ST_MakePoint(longitude::float8, latitude::float8), 4326)::geography))"
        )


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cur:
            cur.execute("DROP INDEX IF EXISTS core_location_geog_gist")


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunPython(enable_postgis, drop_index)]
