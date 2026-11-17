import threading

_local = threading.local()


def get_current_request():
    return getattr(_local, "request", None)


def client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class RequestContextMiddleware:
    """Keeps the current request available for audit logging."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.request = request
        try:
            return self.get_response(request)
        finally:
            _local.request = None


class HealthCheckMiddleware:
    """Answers GET /health and /health/ before host validation and the HTTPS redirect.

    Platform health probes (e.g. PrimeCloud) call the container on its internal
    address over plain HTTP, which would otherwise get a 400 (host not in
    ALLOWED_HOSTS) or a 301 to https. Must be first in MIDDLEWARE.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in ("/health", "/health/") and request.method in ("GET", "HEAD"):
            from django.http import JsonResponse

            return JsonResponse({"status": "ok"})
        return self.get_response(request)


class StorageErrorMiddleware:
    """Turns object-storage failures during web requests into a message instead of a 500.

    The failed save has already been rolled back with its transaction; the user is sent
    back to the page they came from with an error message. API errors are handled in
    apps.core.exceptions.api_exception_handler.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        from apps.core.files import STORAGE_UNAVAILABLE_MESSAGE, is_storage_error

        if not is_storage_error(exception):
            return None
        import logging

        from django.contrib import messages
        from django.shortcuts import redirect
        from django.utils.http import url_has_allowed_host_and_scheme

        logging.getLogger("origin254.files").error("Storage error on %s %s: %s", request.method, request.path, exception)
        messages.error(request, STORAGE_UNAVAILABLE_MESSAGE)
        target = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/app/"
        if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            target = "/app/"
        return redirect(target)
