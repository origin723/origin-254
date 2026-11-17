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
