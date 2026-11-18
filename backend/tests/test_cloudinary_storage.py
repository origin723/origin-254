import io
import urllib.error
from unittest import mock

import cloudinary
import cloudinary.exceptions
from django.core.files.base import ContentFile
from django.test import SimpleTestCase

from apps.core.files import is_storage_error
from apps.core.storage import CloudinaryStorage, StorageUnavailable


class CloudinaryStorageTests(SimpleTestCase):
    def setUp(self):
        cloudinary.config(cloud_name="demo-cloud", api_key="key", api_secret="secret")
        self.storage = CloudinaryStorage(folder="origin254")

    def test_public_id_mapping(self):
        self.assertEqual(self.storage._resource("product_photos/3/ab.JPG"),
                         ("image", "origin254/product_photos/3/ab", "jpg"))
        self.assertEqual(self.storage._resource("verification/5/doc.pdf"),
                         ("raw", "origin254/verification/5/doc.pdf", ""))

    def test_save_uploads_private_asset_and_keeps_name(self):
        with mock.patch("cloudinary.uploader.upload") as upload:
            name = self.storage.save("product_photos/3/ab.png", ContentFile(b"png-bytes", name="ab.png"))
        self.assertEqual(name, "product_photos/3/ab.png")
        kwargs = upload.call_args.kwargs
        self.assertEqual(kwargs["public_id"], "origin254/product_photos/3/ab")
        self.assertEqual(kwargs["resource_type"], "image")
        self.assertEqual(kwargs["type"], "authenticated")
        self.assertFalse(kwargs["overwrite"])

    def test_open_downloads_with_signed_url(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(b"file-bytes")
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            with self.storage.open("verification/5/doc.pdf") as f:
                self.assertEqual(f.read(), b"file-bytes")
        url = urlopen.call_args.args[0]
        self.assertIn("api.cloudinary.com/v1_1/demo-cloud/raw/download", url)  # signed API download
        self.assertIn("signature=", url)
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            self.storage.open("product_photos/1/x.jpg")
        url = urlopen.call_args.args[0]
        self.assertIn("demo-cloud/image/authenticated/s--", url)  # signed CDN delivery for images
        self.assertTrue(url.endswith("origin254/product_photos/1/x.jpg"))

    def test_open_missing_file(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err), self.assertRaises(FileNotFoundError):
            self.storage.open("product_photos/1/x.jpg")

    def test_service_errors_are_storage_errors(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err), self.assertRaises(StorageUnavailable) as ctx:
            self.storage.open("product_photos/1/x.jpg")
        self.assertTrue(is_storage_error(ctx.exception))
        with mock.patch("cloudinary.uploader.upload", side_effect=cloudinary.exceptions.AuthorizationRequired("bad key")):
            with self.assertRaises(StorageUnavailable):
                self.storage.save("product_photos/1/y.png", ContentFile(b"x"))

    def test_exists_and_delete(self):
        with mock.patch("cloudinary.api.resource", return_value={"bytes": 42}):
            self.assertTrue(self.storage.exists("product_photos/1/x.jpg"))
            self.assertEqual(self.storage.size("product_photos/1/x.jpg"), 42)
        with mock.patch("cloudinary.api.resource", side_effect=cloudinary.exceptions.NotFound("nope")):
            self.assertFalse(self.storage.exists("product_photos/1/x.jpg"))
        with mock.patch("cloudinary.uploader.destroy") as destroy:
            self.storage.delete("product_photos/1/x.jpg")
        self.assertEqual(destroy.call_args.args[0], "origin254/product_photos/1/x")
        self.assertEqual(destroy.call_args.kwargs["type"], "authenticated")
