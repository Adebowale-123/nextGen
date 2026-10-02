from django.core.management.base import BaseCommand, CommandError

from apps.ledger.services import verify_ledger_integrity


class Command(BaseCommand):
    help = "Verify every journal transaction balances and every stored balance matches its entries."

    def handle(self, *args, **options):
        problems = verify_ledger_integrity()
        if problems:
            for problem in problems:
                self.stderr.write(problem)
            raise CommandError(f"{len(problems)} ledger problem(s) found.")
        self.stdout.write(self.style.SUCCESS("Ledger OK: all transactions balance and all balances reconcile."))
