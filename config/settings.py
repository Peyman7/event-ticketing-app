"""Settings. Every deploy-specific value comes from the environment (see .env.example)."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "procrastinate.contrib.django",
    "apps.core",
    "apps.catalog",
    "apps.orders",
    "apps.payments",
    "apps.reporting",
]

MIDDLEWARE = [
    "apps.core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL")}
# Persistent connections per gunicorn worker (one sync worker = one connection), with a
# liveness check before reuse. At scale a pooler (PgBouncer) sits in front of Postgres.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="tickets@example.com")

# --- DRF / OpenAPI -----------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "EXCEPTION_HANDLER": "apps.core.exceptions.exception_handler",
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Ticketing API",
    "DESCRIPTION": "Demo ticketing backend: holds, idempotent checkout, payments, check-in.",
    "VERSION": "0.1.0",
    "ENUM_NAME_OVERRIDES": {
        "OrderStatusEnum": "apps.orders.models.OrderStatus",
        "TicketStatusEnum": "apps.orders.models.TicketStatus",
        "PaymentStatusEnum": "apps.payments.models.PaymentStatus",
        "RefundStatusEnum": "apps.payments.models.RefundStatus",
    },
}

# --- Domain settings ---------------------------------------------------------
HOLD_MINUTES = env.int("HOLD_MINUTES", default=10)
PAYMENT_GRACE_MINUTES = env.int("PAYMENT_GRACE_MINUTES", default=15)
IDEMPOTENCY_KEY_TTL_HOURS = env.int("IDEMPOTENCY_KEY_TTL_HOURS", default=24)
PAYMENT_PROVIDER_CLASS = env(
    "PAYMENT_PROVIDER_CLASS", default="apps.payments.providers.fake.FakeProvider"
)
PAYMENT_WEBHOOK_SECRET = env("PAYMENT_WEBHOOK_SECRET")
PAYMENT_WEBHOOK_TOLERANCE_SECONDS = env.int("PAYMENT_WEBHOOK_TOLERANCE_SECONDS", default=300)
FAKE_PROVIDER_WEBHOOK_URL = env(
    "FAKE_PROVIDER_WEBHOOK_URL", default="http://web:8000/api/webhooks/payments/"
)
FAKE_PROVIDER_DELAY_SECONDS = env.int("FAKE_PROVIDER_DELAY_SECONDS", default=30)

# --- Observability -----------------------------------------------------------
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOG_JSON = env.bool("LOG_JSON", default=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"context": {"()": "apps.core.logging.ContextFilter"}},
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.json.JsonFormatter",
            "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s",
            "rename_fields": {"asctime": "ts", "levelname": "level", "name": "logger"},
        },
        "plain": {
            "format": "%(asctime)s %(levelname)s %(name)s [rid=%(request_id)s "
            "order=%(order_id)s] %(message)s"
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["context"],
            "formatter": "json" if LOG_JSON else "plain",
        }
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "procrastinate": {"level": "WARNING"},
    },
}

SENTRY_DSN = env("SENTRY_DSN", default="")
SENTRY_ENVIRONMENT = env("SENTRY_ENVIRONMENT", default="dev")
