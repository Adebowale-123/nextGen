"""
Django settings for NextGen Game.

All secrets and environment-specific values come from environment variables
(loaded from a local .env file in development). See .env.example.
"""

import os
import sys
from decimal import Decimal
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    value = os.environ.get(key)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_int(key, default):
    value = os.environ.get(key)
    return int(value) if value not in (None, "") else default


def env_list(key, default=""):
    return [item.strip() for item in env(key, default).split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

DEBUG = env_bool("DEBUG", True)
SECRET_KEY = env("SECRET_KEY", "dev-insecure-key-change-me-before-deploying")
if not DEBUG and SECRET_KEY.startswith("dev-insecure"):
    raise RuntimeError("SECRET_KEY must be set in production.")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
SITE_URL = env("SITE_URL", "http://127.0.0.1:8000").rstrip("/")

# Render sets these automatically for every web service.
RENDER_HOSTNAME = env("RENDER_EXTERNAL_HOSTNAME")
if RENDER_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_HOSTNAME)
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_HOSTNAME}")
    SITE_URL = env("SITE_URL", f"https://{RENDER_HOSTNAME}").rstrip("/")

# Public demo: shows a "no real money" banner and displays sign-up codes on screen
# (because no SMS/email provider is connected). Must be off for a real launch.
DEMO_MODE = env_bool("DEMO_MODE", False)

# Free hosting has no always-on background worker, so the website itself advances
# games (open/close batches, settle) at most every SCHEDULER_ON_REQUEST_SECONDS.
SCHEDULER_ON_REQUEST = env_bool("SCHEDULER_ON_REQUEST", False)
SCHEDULER_ON_REQUEST_SECONDS = env_int("SCHEDULER_ON_REQUEST_SECONDS", 20)

INSTALLED_APPS = [
    "django.contrib.admin",
    "apps.core.apps.StaffAccessConfig",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "apps.core",
    "apps.accounts",
    "apps.ledger",
    "apps.compliance",
    "apps.notifications",
    "apps.payments",
    "apps.games",
    "apps.backoffice",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.middleware.IdleSessionTimeoutMiddleware",
    "apps.core.middleware.RequestDrivenSchedulerMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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
                "apps.core.context_processors.platform",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# PostgreSQL in every real environment: set DATABASE_URL, e.g.
#   postgres://nextgen:password@localhost:5432/nextgen
# Falls back to SQLite so the project runs before Postgres is configured.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Use Redis in production (CACHE_URL is not parsed here; swap the backend
# to django.core.cache.backends.redis.RedisCache with LOCATION=redis://...).
CACHES = {
    "default": {
        "BACKEND": env("CACHE_BACKEND", "django.core.cache.backends.locmem.LocMemCache"),
        "LOCATION": env("CACHE_LOCATION", "nextgen"),
    }
}

# Django 6 background tasks. ImmediateBackend runs tasks inline; point this at
# a worker-backed backend in production so payouts/notifications run async.
TASKS = {
    "default": {
        "BACKEND": env("TASKS_BACKEND", "django.tasks.backends.immediate.ImmediateBackend"),
    }
}

# ---------------------------------------------------------------------------
# Auth & sessions
# ---------------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.IdentifierBackend"]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"
LOGOUT_REDIRECT_URL = "core:home"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

SESSION_COOKIE_AGE = 60 * 60 * 12  # absolute lifetime: 12 hours
SESSION_IDLE_TIMEOUT = env_int("SESSION_IDLE_TIMEOUT", 15 * 60)  # logout after 15 min idle
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"

GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", "")

# ---------------------------------------------------------------------------
# I18N / static / media
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-ng"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG else "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# KYC documents live here. They are never served publicly; staff download them
# through an access-controlled view.
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "private_media")))
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# ---------------------------------------------------------------------------
# Notifications (email / SMS)
# ---------------------------------------------------------------------------

EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "NextGen Game <no-reply@nextgen.game>")

SMS_BACKEND = env("SMS_BACKEND", "console")  # console | termii
TERMII_API_KEY = env("TERMII_API_KEY", "")
TERMII_SENDER_ID = env("TERMII_SENDER_ID", "NextGen")
TERMII_BASE_URL = env("TERMII_BASE_URL", "https://v3.api.termii.com")

# ---------------------------------------------------------------------------
# KYC
# ---------------------------------------------------------------------------

KYC_PROVIDER = env("KYC_PROVIDER", "mock")  # mock | dojah
DOJAH_APP_ID = env("DOJAH_APP_ID", "")
DOJAH_SECRET_KEY = env("DOJAH_SECRET_KEY", "")
DOJAH_BASE_URL = env("DOJAH_BASE_URL", "https://sandbox.dojah.io")

# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", "").strip()
PAYSTACK_PUBLIC_KEY = env("PAYSTACK_PUBLIC_KEY", "").strip()
# Which checkout players use: mock (sandbox page), paystack or flutterwave. When not set explicitly,
# Paystack is used as soon as its secret key is present, otherwise the sandbox.
PAYMENT_PROVIDERS = env_list("PAYMENT_PROVIDERS") or ["paystack" if PAYSTACK_SECRET_KEY else "mock"]
# Who sends withdrawals. Stays on the sandbox unless set (Paystack transfers need extra account setup).
PAYOUT_PROVIDER = (env("PAYOUT_PROVIDER") or "mock").strip()
FLUTTERWAVE_SECRET_KEY = env("FLUTTERWAVE_SECRET_KEY", "")
FLUTTERWAVE_WEBHOOK_HASH = env("FLUTTERWAVE_WEBHOOK_HASH", "")
MOCK_GATEWAY_SECRET = env("MOCK_GATEWAY_SECRET", SECRET_KEY + ":mock-gateway")

# ---------------------------------------------------------------------------
# Platform rules. Amounts are in minor units (kobo for NGN).
# ---------------------------------------------------------------------------

TICKET_SIGNING_KEY = env("TICKET_SIGNING_KEY", SECRET_KEY + ":tickets")

NEXTGEN = {
    "BRAND_NAME": "NextGen Game",
    "DEFAULT_CURRENCY": env("DEFAULT_CURRENCY", "NGN"),
    "SUPPORTED_CURRENCIES": env_list("SUPPORTED_CURRENCIES", "NGN"),
    "MIN_AGE": 18,
    # AML: deposits must be wagered this many times before withdrawal.
    "AML_WAGER_MULTIPLIER": Decimal(env("AML_WAGER_MULTIPLIER", "1.0")),
    # Deposits
    "MIN_DEPOSIT": env_int("MIN_DEPOSIT", 1_000_00),
    "MAX_DEPOSIT": env_int("MAX_DEPOSIT", 1_000_000_00),
    # Coin packages shown on the Buy coins page.
    "COIN_PACKAGES": "100,300,500,1000,2000,5000",
    "DEFAULT_DAILY_DEPOSIT_LIMIT": env_int("DEFAULT_DAILY_DEPOSIT_LIMIT", 500_000_00),
    "TIER1_DAILY_DEPOSIT_LIMIT": env_int("TIER1_DAILY_DEPOSIT_LIMIT", 50_000_00),
    # Tier 1 players may only buy low-stake tickets: max total per purchase.
    "TIER1_MAX_STAKE_PER_PURCHASE": env_int("TIER1_MAX_STAKE_PER_PURCHASE", 2_000_00),
    # Withdrawals
    "MIN_WITHDRAWAL": env_int("MIN_WITHDRAWAL", 1_000_00),
    "WITHDRAWAL_REVIEW_THRESHOLD": env_int("WITHDRAWAL_REVIEW_THRESHOLD", 500_000_00),
    "MAX_WITHDRAWALS_PER_DAY": env_int("MAX_WITHDRAWALS_PER_DAY", 3),
    "NEW_ACCOUNT_REVIEW_HOURS": env_int("NEW_ACCOUNT_REVIEW_HOURS", 24),
    # Spin games: auto-spin a closed batch if no admin has spun it after N minutes (0 = admin only).
    "AUTO_SPIN_AFTER_MINUTES": env_int("AUTO_SPIN_AFTER_MINUTES", 0),
    # Welcome bonus credited when a new player verifies their account (play-only, not withdrawable).
    "WELCOME_BONUS": env_int("WELCOME_BONUS", 500_00),
    # Players see coins: 1 coin is worth this many kobo (₦10 by default).
    "COIN_VALUE": env_int("COIN_VALUE", 10_00),
    # KYC passes only when the name on the ID matches the bank account name.
    "KYC_REQUIRE_BANK_NAME_MATCH": True,
    # Responsible gaming
    "LIMIT_INCREASE_COOLDOWN_HOURS": env_int("LIMIT_INCREASE_COOLDOWN_HOURS", 24),
    # OTP
    "OTP_TTL_SECONDS": env_int("OTP_TTL_SECONDS", 300),
    "OTP_MAX_ATTEMPTS": env_int("OTP_MAX_ATTEMPTS", 5),
    "OTP_RESEND_COOLDOWN_SECONDS": env_int("OTP_RESEND_COOLDOWN_SECONDS", 60),
    # Login brute-force protection
    "LOGIN_MAX_ATTEMPTS": env_int("LOGIN_MAX_ATTEMPTS", 5),
    "LOGIN_LOCKOUT_SECONDS": env_int("LOGIN_LOCKOUT_SECONDS", 15 * 60),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "apps": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    },
}

TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
if TESTING:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    LOGGING["loggers"]["apps"]["level"] = "WARNING"
