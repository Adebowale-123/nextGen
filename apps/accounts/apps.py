from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = 'apps.accounts'
    verbose_name = 'Players'

    def ready(self):
        from . import signals  # noqa: F401
