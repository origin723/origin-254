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
