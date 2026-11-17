from django.core.management import call_command
from django.test import TestCase

from apps.core.models import Location


class KenyaLocationsTests(TestCase):
    def test_all_counties_and_constituencies_load(self):
        call_command("load_kenya_locations", stdout=open("/dev/null", "w"))
        self.assertEqual(Location.objects.values("county").distinct().count(), 47)
        self.assertEqual(Location.objects.filter(name__endswith=" County", sub_county="").count(), 47)
        self.assertEqual(Location.objects.exclude(sub_county="").count(), 290)
        self.assertEqual(Location.objects.filter(county="Nairobi").exclude(sub_county="").count(), 17)
        self.assertTrue(Location.objects.filter(county="Elgeyo-Marakwet", name="Marakwet West").exists())
        self.assertTrue(Location.objects.filter(county="Bungoma", name="Webuye West").exists())
        for loc in Location.objects.all():  # every point lies within Kenya's bounding box
            self.assertTrue(-4.8 < loc.latitude < 5.1 and 33.9 < loc.longitude < 41.95, loc)

    def test_loading_twice_does_not_duplicate(self):
        call_command("load_kenya_locations", stdout=open("/dev/null", "w"))
        n = Location.objects.count()
        call_command("load_kenya_locations", stdout=open("/dev/null", "w"))
        self.assertEqual(Location.objects.count(), n)
