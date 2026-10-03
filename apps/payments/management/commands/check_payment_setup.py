from django.conf import settings
from django.core.management.base import BaseCommand

from apps.payments.providers import check_provider, payment_mode


class Command(BaseCommand):
    help = "Check the payment provider keys with a read-only call. Never prints the keys."

    def handle(self, *args, **options):
        self.stdout.write(f"Payment mode: {payment_mode().upper()}")
        codes = list(dict.fromkeys([*settings.PAYMENT_PROVIDERS, settings.PAYOUT_PROVIDER]))
        all_ok = True
        for code in codes:
            role = []
            if code in settings.PAYMENT_PROVIDERS:
                role.append("coin purchases")
            if code == settings.PAYOUT_PROVIDER:
                role.append("withdrawals")
            ok, message = check_provider(code)
            all_ok &= ok
            style = self.style.SUCCESS if ok else self.style.ERROR
            self.stdout.write(style(f"  {code} ({' + '.join(role)}): {'OK' if ok else 'PROBLEM'} - {message}"))
        if not all_ok:
            self.stdout.write(self.style.WARNING("Fix the problem above in Render -> nextgen-game -> Environment."))
