from django.core.files.base import ContentFile
from django.test import TestCase

from apps.core.models import StoredFile
from apps.core.storage import DatabaseStorage


class DatabaseStorageTests(TestCase):
    def setUp(self):
        self.storage = DatabaseStorage()

    def test_save_open_size_delete(self):
        name = self.storage.save("product_photos/1/a.png", ContentFile(b"\x89PNGdata", name="a.png"))
        self.assertEqual(name, "product_photos/1/a.png")
        self.assertTrue(self.storage.exists(name))
        self.assertEqual(self.storage.size(name), 8)
        with self.storage.open(name) as f:
            self.assertEqual(f.read(), b"\x89PNGdata")
        self.storage.delete(name)
        self.assertFalse(self.storage.exists(name))
        self.assertFalse(StoredFile.objects.exists())

    def test_name_collision_gets_new_name(self):
        first = self.storage.save("docs/x.pdf", ContentFile(b"one"))
        second = self.storage.save("docs/x.pdf", ContentFile(b"two"))
        self.assertNotEqual(first, second)
        with self.storage.open(first) as f:
            self.assertEqual(f.read(), b"one")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            self.storage.open("nope.png")
        self.storage.delete("nope.png")  # no error

    def test_listdir(self):
        self.storage.save("product_photos/1/a.png", ContentFile(b"a"))
        self.storage.save("product_photos/2/b.png", ContentFile(b"b"))
        self.assertEqual(self.storage.listdir("product_photos"), (["1", "2"], []))
        self.assertEqual(self.storage.listdir("product_photos/1"), ([], ["a.png"]))
