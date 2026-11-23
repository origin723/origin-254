import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryItem, InventoryPhoto

from .factories import api, mango_request, seed, user

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
TMP = tempfile.mkdtemp()


def png(name="mango.png", data=PNG):
    return SimpleUploadedFile(name, data, content_type="image/png")


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage",
                                         "OPTIONS": {"location": TMP}},
                             "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class ProductPhotoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.item = InventoryItem.objects.get(supplier__user__username="makueni_coop")
        self.base = self.item.photos.count()  # demo listings start with the required 2 photos
        self.client = Client()
        self.client.force_login(user("makueni_coop"))

    def upload(self, *files):
        return self.client.post(f"/app/inventory/{self.item.pk}/photos/", {"photos": list(files)}, follow=True)

    def test_dashboard_has_photo_upload_section(self):
        html = self.client.get("/app/").content.decode()
        self.assertIn("Product photos", html)
        self.assertIn(f"/app/inventory/{self.item.pk}/photos/", html)
        self.assertIn('type="file" name="photos"', html)

    def test_upload_multiple_photos(self):
        self.upload(png("a.png"), SimpleUploadedFile("b.jpg", JPEG, content_type="image/jpeg"))
        self.assertEqual(self.item.photos.count(), self.base + 2)

    def test_rejects_non_images_even_with_image_extension(self):
        r = self.upload(png("fake.png", b"MZ this is an exe"))
        self.assertContains(r, "Upload a JPG, PNG or WebP photo")
        self.assertEqual(self.item.photos.count(), self.base)

    def test_limit_of_six_per_listing(self):
        self.upload(*[png(f"{i}.png") for i in range(6 - self.base)])
        r = self.upload(png("7.png"))
        self.assertEqual(self.item.photos.count(), 6)
        self.assertContains(r, "up to 6 photos")

    def test_cannot_upload_to_someone_elses_stock(self):
        other = InventoryItem.objects.get(supplier__user__username="machakos_agg")
        r = self.client.post(f"/app/inventory/{other.pk}/photos/", {"photos": [png()]})
        self.assertEqual(r.status_code, 404)

    def test_photo_visibility(self):
        photo = self.item.photos.first()
        url = f"/app/photos/{photo.pk}/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url)["Content-Type"], "image/png")
        buyer = Client()
        buyer.force_login(user("mama_jam"))
        self.assertEqual(buyer.get(url).status_code, 200, "buyers see photos of listed stock")
        req, _ = mango_request()
        # Hidden / sold-out listing: only buyers who dealt with it still see it
        self.item.is_active = False
        self.item.save()
        self.assertEqual(buyer.get(url).status_code, 200, "matched buyer keeps access")
        other_buyer = Client()
        other_buyer.force_login(user("new_juice_co"))
        self.assertEqual(other_buyer.get(url).status_code, 404, "unrelated buyer, hidden listing")
        self.item.is_active = True
        self.item.save()
        stranger = Client()
        stranger.force_login(user("machakos_agg"))
        self.assertEqual(stranger.get(url).status_code, 404)
        self.assertEqual(Client().get(url).status_code, 302)

    def test_buyer_sees_thumbnails_on_matches(self):
        req, _ = mango_request()
        buyer = Client()
        buyer.force_login(user("mama_jam"))
        html = buyer.get(f"/app/requests/{req.pk}/").content.decode()
        for photo in self.item.photos.all():
            self.assertIn(f"/app/photos/{photo.pk}/", html)

    def test_delete_photo_allowed_above_minimum(self):
        self.upload(png())
        photo = self.item.photos.last()
        self.client.post(f"/app/photos/{photo.pk}/delete/")
        self.assertFalse(InventoryPhoto.objects.filter(pk=photo.pk).exists())

    def test_delete_below_two_photos_warns_and_hides_listing(self):
        from unittest import mock

        from django.core.files.storage import FileSystemStorage
        photo = self.item.photos.first()
        with mock.patch.object(FileSystemStorage, "delete"):  # keep the shared fixture file for other tests
            r = self.client.post(f"/app/photos/{photo.pk}/delete/", follow=True)
        self.assertContains(r, "hidden from buyers until you add 1 more photo")
        self.assertFalse(InventoryPhoto.objects.filter(pk=photo.pk).exists())
        self.assertFalse(self.item.has_required_photos)

    def test_delete_photo_whose_file_is_gone(self):
        self.upload(png())  # its own photo: deleting a shared fixture file would leak into other tests
        photo = self.item.photos.last()
        photo.image.storage.delete(photo.image.name)  # e.g. lost on an old server or never uploaded
        self.client.post(f"/app/photos/{photo.pk}/delete/")
        self.assertFalse(InventoryPhoto.objects.filter(pk=photo.pk).exists())

    def test_delete_photo_when_storage_fails(self):
        from unittest import mock

        from django.core.files.storage import FileSystemStorage
        photo = self.item.photos.first()
        with mock.patch.object(FileSystemStorage, "delete", side_effect=OSError("storage down")):
            r = self.client.post(f"/app/photos/{photo.pk}/delete/", follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(InventoryPhoto.objects.filter(pk=photo.pk).exists())

    def test_listing_without_two_photos_is_hidden_from_buyers(self):
        from apps.catalog.models import Product
        from apps.core.models import Location
        from apps.sourcing.matching import nearest_suppliers
        keep = self.item.photos.first()
        self.item.photos.exclude(pk=keep.pk).delete()  # down to 1 photo
        req, _ = mango_request()
        self.assertFalse(req.matches.filter(inventory_item=self.item).exists())
        near = nearest_suppliers(Product.objects.get(name="Mango"), Location.objects.get(name="Nairobi Industrial Area"))
        self.assertNotIn(self.item, [r.item for r in near])
        self.upload(png())  # back to 2 -> visible again
        from apps.sourcing.matching import run_matching
        run_matching(req)
        self.assertTrue(req.matches.filter(inventory_item=self.item).exists())

    def test_quoting_requires_photos(self):
        from apps.core.exceptions import WorkflowError
        from .factories import quote
        req, _ = mango_request()
        keep = self.item.photos.first()
        self.item.photos.exclude(pk=keep.pk).delete()
        with self.assertRaises(WorkflowError):
            quote(req, "makueni_coop", "100", "45")

    def test_dashboard_warns_about_missing_photos(self):
        keep = self.item.photos.first()
        self.item.photos.exclude(pk=keep.pk).delete()
        self.assertContains(self.client.get("/app/"), "needs at least 2 photos")

    def test_api_upload(self):
        r = api("makueni_coop").post(f"/api/v1/inventory/{self.item.pk}/photos/",
                                     {"photos": [png()], "caption": "Fresh"}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.data[0]["caption"], "Fresh")
        data = api("makueni_coop").get(f"/api/v1/inventory/{self.item.pk}/").data
        self.assertEqual(len(data["photos"]), self.base + 1)
        self.assertEqual(data["photos_missing"], 0)


class NearestSupplierPhotoTests(TestCase):
    """Regression: photos in nearest-supplier results were broken for buyers not yet matched."""

    @classmethod
    def setUpTestData(cls):
        seed()

    def test_unmatched_buyer_sees_result_photos(self):
        c = Client()
        c.force_login(user("mama_jam"))
        from apps.core.models import Location
        r = c.get("/app/suppliers/nearest/", {"product": "Mango",
                                               "location": Location.objects.get(name="Nairobi Industrial Area").pk})
        import re
        urls = re.findall(r'src="(/app/photos/\d+/(?:\?size=thumb)?)"', r.content.decode())
        self.assertTrue(urls)
        for url in urls:
            resp = c.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertTrue(resp["Content-Type"].startswith("image/"))

    def test_collection_only_badge(self):
        from apps.accounts.models import SupplierProfile
        SupplierProfile.objects.filter(user__username="machakos_agg").update(can_deliver=False, delivery_radius_km=0)
        c = Client()
        c.force_login(user("mama_jam"))
        from apps.core.models import Location
        r = c.get("/app/suppliers/nearest/", {"product": "Mango",
                                               "location": Location.objects.get(name="Nairobi Industrial Area").pk})
        self.assertContains(r, "Collection only")
        self.assertNotContains(r, "Up to 0 km")
        self.assertContains(r, " kilograms</td>")

    def test_photo_view_redirects_to_signed_cdn_url_when_storage_offers_one(self):
        from unittest import mock

        from django.core.files.storage import FileSystemStorage
        from apps.catalog.models import InventoryPhoto
        photo = InventoryPhoto.objects.select_related("inventory_item__supplier__user").first()
        c = Client()
        c.force_login(photo.inventory_item.supplier.user)
        signer = mock.Mock(return_value="https://cdn.example/signed/x.jpg")
        with mock.patch.object(FileSystemStorage, "signed_image_url", signer, create=True):
            r = c.get(f"/app/photos/{photo.pk}/?size=thumb")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "https://cdn.example/signed/x.jpg")
        self.assertEqual(signer.call_args.kwargs["width"], 200)

