from django.db import migrations, models


def to_farmer(apps, schema_editor):
    apps.get_model("accounts", "User").objects.filter(role__in=["cooperative", "aggregator"]).update(role="farmer")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_initial")]

    operations = [
        migrations.RunPython(to_farmer, migrations.RunPython.noop),
    ]
