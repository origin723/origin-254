"""Django settings for Origin 254.

Configuration is read from environment variables (and a local `.env` file in
development). See `.env.example` for every supported variable.
"""
import os
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
PROJECT_ROOT = BASE_DIR.parent  # repo root: .env, runtime data dirs


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(PROJECT_ROOT / ".env")


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    return str(env(key, "1" if default else "0")).lower() in {"1", "true", "yes", "on"}


def env_list(key, default=""):
    return [v.strip() for v in str(env(key, default)).split(",") if v.strip()]


def database_from_url(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme in {"sqlite", "sqlite3"}:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": parsed.path.lstrip("/") or PROJECT_ROOT / "db.sqlite3"}
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "CONN_MAX_AGE": 60,
    }


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
# Set to 1 only if the platform proxy rewrites Host and passes the public one in X-Forwarded-Host.
USE_X_FORWARDED_HOST = env_bool("DJANGO_USE_X_FORWARDED_HOST", False)
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "drf_spectacular",
    "corsheaders",
    "apps.core",
    "apps.accounts",
    "apps.catalog",
    "apps.sourcing",
    "apps.orders",
    "apps.comms",
    "apps.reviews",
    "apps.payments",
    "apps.web",
]

MIDDLEWARE = [
    "apps.core.middleware.HealthCheckMiddleware",  # first: probes bypass host check / SSL redirect
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.RequestContextMiddleware",
]

ROOT_URLCONF = "origin254.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.web.context.unread_counts",
            ],
        },
    },
]

WSGI_APPLICATION = "origin254.wsgi.application"

DATABASES = {"default": database_from_url(env("DATABASE_URL", "postgres://localhost:5432/origin254"))}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = PROJECT_ROOT / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Verification documents, inspection reports and proofs of delivery are stored
# outside any publicly served directory and only streamed through
# permission-checked views.
PRIVATE_MEDIA_ROOT = Path(env("PRIVATE_MEDIA_ROOT", PROJECT_ROOT / "private_media"))
if "test" in sys.argv[1:2]:  # keep files written by the test suite out of the real media folder
    import tempfile
    PRIVATE_MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="origin254-test-media-"))
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": PRIVATE_MEDIA_ROOT}},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"
                    if not DEBUG else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
MAX_DOCUMENT_SIZE_MB = 10
ALLOWED_DOCUMENT_EXTENSIONS = ["pdf", "jpg", "jpeg", "png"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "web:login"
LOGIN_REDIRECT_URL = "web:dashboard"
LOGOUT_REDIRECT_URL = "web:landing"

# --- REST API -------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
        "rest_framework.filters.SearchFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {"anon": "60/min", "user": "600/min"},
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Origin 254 API",
    "DESCRIPTION": "Agricultural sourcing and value-addition marketplace API (Kenya).",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "QualityGradeEnum": "apps.catalog.models.QualityGrade",
        "UnitEnum": "apps.catalog.models.Unit",
        "VerificationStatusEnum": "apps.accounts.models.VerificationStatus",
        "SourcingRequestStatusEnum": "apps.sourcing.models.SourcingRequest.Status",
        "MatchStatusEnum": "apps.sourcing.models.Match.Status",
        "QuotationStatusEnum": "apps.sourcing.models.Quotation.Status",
        "OrderStatusEnum": "apps.orders.models.Order.Status",
        "OrderLineStatusEnum": "apps.orders.models.OrderLine.Status",
        "DeliveryStatusEnum": "apps.orders.models.Delivery.Status",
        "DisputeStatusEnum": "apps.orders.models.Dispute.Status",
        "InspectionResultEnum": "apps.orders.models.Inspection.Result",
        
    },
}

CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "")

# --- Notifications ----------------------------------------------------------
NOTIFICATION_CHANNELS = env_list("NOTIFICATION_CHANNELS", "in_app,email")
SMS_PROVIDER = env("SMS_PROVIDER", "console")
WHATSAPP_PROVIDER = env("WHATSAPP_PROVIDER", "console")
AFRICASTALKING_USERNAME = env("AFRICASTALKING_USERNAME", "")
AFRICASTALKING_API_KEY = env("AFRICASTALKING_API_KEY", "")
TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = env("TWILIO_WHATSAPP_FROM", "")
TWILIO_SMS_FROM = env("TWILIO_SMS_FROM", "")
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "Origin 254 <no-reply@origin254.example>")
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", 587))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)

# --- Payments (M-Pesa) ---------------------------------------------------------
# simulated: no money moves, payments succeed instantly (development / demos).
# mpesa: Safaricom Daraja STK Push. Needs the MPESA_* values below and a public HTTPS
# MPESA_CALLBACK_BASE_URL that Safaricom can reach.
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", "simulated")
# How suppliers are paid once the buyer confirms delivery:
# manual (admin pays and records the transaction code), simulated, or mpesa (B2C).
PAYOUT_PROVIDER = env("PAYOUT_PROVIDER", "manual")
# Origin 254 commission, kept from every supplier payout; the supplier receives the rest.
PLATFORM_FEE_PERCENT = env("PLATFORM_FEE_PERCENT", "10")
# When 1, suppliers/logistics cannot mark a lot as picked up until the buyer has paid.
REQUIRE_PAYMENT_BEFORE_DISPATCH = env_bool("REQUIRE_PAYMENT_BEFORE_DISPATCH", False)
MPESA_ENVIRONMENT = env("MPESA_ENVIRONMENT", "sandbox")  # sandbox or production
MPESA_CONSUMER_KEY = env("MPESA_CONSUMER_KEY", "")
MPESA_CONSUMER_SECRET = env("MPESA_CONSUMER_SECRET", "")
MPESA_SHORTCODE = env("MPESA_SHORTCODE", "")
MPESA_PASSKEY = env("MPESA_PASSKEY", "")
MPESA_TRANSACTION_TYPE = env("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline")  # or CustomerBuyGoodsOnline
MPESA_TILL_OR_PAYBILL = env("MPESA_TILL_OR_PAYBILL", "")  # PartyB if different from the shortcode (till)
MPESA_CALLBACK_BASE_URL = env("MPESA_CALLBACK_BASE_URL", "")
MPESA_CALLBACK_TOKEN = env("MPESA_CALLBACK_TOKEN", "")  # long random string; part of the callback URL
MPESA_B2C_SHORTCODE = env("MPESA_B2C_SHORTCODE", "")
MPESA_B2C_INITIATOR = env("MPESA_B2C_INITIATOR", "")
MPESA_B2C_SECURITY_CREDENTIAL = env("MPESA_B2C_SECURITY_CREDENTIAL", "")

# --- Business rules -----------------------------------------------------------
# When False (default), new accounts are approved as soon as they register and can
# trade immediately; admins can still reject or suspend them later. Set to 1 to make
# every new account wait for admin verification.
REQUIRE_ACCOUNT_VERIFICATION = env_bool("REQUIRE_ACCOUNT_VERIFICATION", False)
# Stock listings need at least this many product photos before buyers can see them.
MIN_PHOTOS_PER_LISTING = int(env("MIN_PHOTOS_PER_LISTING", 2))
# Inventory not reconfirmed within this many days is treated as stale and is
# excluded from matching until the supplier updates it.
INVENTORY_STALE_DAYS = int(env("INVENTORY_STALE_DAYS", 14))

# --- Backups ----------------------------------------------------------------
BACKUP_DIR = Path(env("BACKUP_DIR", PROJECT_ROOT / "backups"))
BACKUP_RETENTION_DAYS = int(env("BACKUP_RETENTION_DAYS", 14))

# --- Security (production) ----------------------------------------------------
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_CONTENT_TYPE_NOSNIFF = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "origin254": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
        # Django warnings/errors (e.g. "Invalid HTTP_HOST header") go to stdout for platform logs
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
