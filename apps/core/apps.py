from django.apps import AppConfig
from django.contrib.auth.apps import AuthConfig as DjangoAuthConfig


class CoreConfig(AppConfig):
    name = 'apps.core'
    verbose_name = 'Settings'
    default = True


class StaffAccessConfig(DjangoAuthConfig):
    """Django's auth app, shown as 'Staff access' in the admin menu (used in INSTALLED_APPS)."""

    verbose_name = "Staff access"
    default = False
