from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import User


@receiver(post_save, sender=User)
def create_wallets_for_new_user(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        from apps.ledger.services import create_wallet

        for currency in settings.NEXTGEN["SUPPORTED_CURRENCIES"]:
            create_wallet(instance, currency)
