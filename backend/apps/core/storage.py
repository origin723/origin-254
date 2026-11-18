"""Django storage backend that keeps uploaded files in the database (core.StoredFile)."""
from io import BytesIO

from django.core.files.base import File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class DatabaseStorage(Storage):
    def _model(self):
        from apps.core.models import StoredFile

        return StoredFile

    def _open(self, name, mode="rb"):
        if "w" in mode or "a" in mode:
            raise ValueError("DatabaseStorage files are read-only once saved")
        try:
            row = self._model().objects.only("content").get(name=name)
        except self._model().DoesNotExist:
            raise FileNotFoundError(name) from None
        f = File(BytesIO(bytes(row.content)), name=name)
        f.mode = mode
        return f

    def _save(self, name, content):
        if hasattr(content, "seek"):
            content.seek(0)
        data = b"".join(content.chunks()) if hasattr(content, "chunks") else content.read()
        self._model().objects.create(name=name, content=data, size=len(data))
        return name

    def delete(self, name):
        self._model().objects.filter(name=name).delete()

    def exists(self, name):
        return self._model().objects.filter(name=name).exists()

    def size(self, name):
        try:
            return self._model().objects.values_list("size", flat=True).get(name=name)
        except self._model().DoesNotExist:
            raise FileNotFoundError(name) from None

    def listdir(self, path):
        prefix = f"{path.rstrip('/')}/" if path else ""
        dirs, files = set(), []
        for name in self._model().objects.filter(name__startswith=prefix).values_list("name", flat=True):
            rest = name[len(prefix):]
            if "/" in rest:
                dirs.add(rest.split("/", 1)[0])
            else:
                files.append(rest)
        return sorted(dirs), sorted(files)

    def get_created_time(self, name):
        return self._model().objects.values_list("created_at", flat=True).get(name=name)

    def get_modified_time(self, name):
        return self.get_created_time(name)

    def url(self, name):
        raise NotImplementedError("Files are served through permission-checked views, not public URLs")


class StorageUnavailable(Exception):
    """The remote media service could not be reached or refused the request."""


IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}


@deconstructible
class CloudinaryStorage(Storage):
    """Stores uploads as private ("authenticated") Cloudinary assets.

    Images are Cloudinary image resources (visible in the Media Library); other files
    (PDF documents, reports) are raw resources so their bytes are kept exactly.
    Nothing is publicly reachable: views download the file with a signed URL and
    stream it to the user after their permission checks. Configured by CLOUDINARY_URL
    (cloudinary://API_KEY:API_SECRET@CLOUD_NAME); CLOUDINARY_FOLDER prefixes every file.
    """

    delivery_type = "authenticated"

    def __init__(self, folder=None):
        from django.conf import settings

        self.folder = (folder if folder is not None else getattr(settings, "CLOUDINARY_FOLDER", "")).strip("/")

    # name <-> Cloudinary public_id ------------------------------------------------
    def _resource(self, name):
        stem, _, ext = name.rpartition(".")
        ext = ext.lower() if stem else ""
        path = f"{self.folder}/{name}" if self.folder else name
        if ext in IMAGE_EXTENSIONS:
            return "image", path[: -(len(ext) + 1)], ext  # image public_ids carry no extension
        return "raw", path, ""

    def _call(self, fn, *args, **kwargs):
        import cloudinary.exceptions

        try:
            return fn(*args, **kwargs)
        except cloudinary.exceptions.NotFound:
            raise FileNotFoundError(args[0] if args else "") from None
        except cloudinary.exceptions.Error as exc:
            raise StorageUnavailable(f"Cloudinary: {exc}") from exc

    # Storage API --------------------------------------------------------------------
    def _save(self, name, content):
        import cloudinary.uploader

        resource_type, public_id, _ = self._resource(name)
        if hasattr(content, "seek"):
            content.seek(0)
        self._call(cloudinary.uploader.upload, content, public_id=public_id, resource_type=resource_type,
                   type=self.delivery_type, overwrite=False, unique_filename=False, use_filename=False)
        return name

    def _open(self, name, mode="rb"):
        import urllib.error
        import urllib.request

        import cloudinary.utils

        if "w" in mode or "a" in mode:
            raise ValueError("Cloudinary files are read-only once saved")
        resource_type, public_id, ext = self._resource(name)
        if resource_type == "image":  # signed CDN delivery URL
            url, _ = cloudinary.utils.cloudinary_url(public_id, resource_type=resource_type, type=self.delivery_type,
                                                     format=ext, sign_url=True, secure=True)
        else:  # signed API download: CDN delivery of PDFs/ZIPs is blocked on many accounts by default
            url = cloudinary.utils.private_download_url(public_id, "", resource_type=resource_type,
                                                        type=self.delivery_type)
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(name) from None
            raise StorageUnavailable(f"Cloudinary download failed ({exc.code}) for {name}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise StorageUnavailable(f"Cloudinary unreachable: {exc}") from exc
        f = File(BytesIO(data), name=name)
        f.mode = mode
        return f

    def _info(self, name):
        import cloudinary.api

        resource_type, public_id, _ = self._resource(name)
        return self._call(cloudinary.api.resource, public_id, resource_type=resource_type, type=self.delivery_type)

    def exists(self, name):
        try:
            self._info(name)
            return True
        except FileNotFoundError:
            return False

    def size(self, name):
        return self._info(name)["bytes"]

    def delete(self, name):
        import cloudinary.uploader

        resource_type, public_id, _ = self._resource(name)
        self._call(cloudinary.uploader.destroy, public_id, resource_type=resource_type,
                   type=self.delivery_type, invalidate=True)

    def get_available_name(self, name, max_length=None):
        # Upload paths already contain a random UUID, so skip the per-save existence check
        # (it costs an Admin API call, which Cloudinary rate-limits).
        return name

    def listdir(self, path):
        raise NotImplementedError("Listing is not supported for Cloudinary storage")

    def url(self, name):
        raise NotImplementedError("Files are served through permission-checked views, not public URLs")
