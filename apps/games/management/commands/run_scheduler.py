import time

from django.core.management.base import BaseCommand

from apps.games.services import run_scheduler_tick
from apps.payments.services import expire_stale_deposits


class Command(BaseCommand):
    help = "Run the draw scheduler: close sales, draw numbers, settle, open the next draw."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single tick and exit (for cron/Task Scheduler).")
        parser.add_argument("--interval", type=int, default=10, help="Seconds between ticks (default 10).")

    def handle(self, *args, once=False, interval=10, **options):
        self.stdout.write(self.style.SUCCESS("Draw scheduler started." if not once else "Running one tick."))
        ticks = 0
        while True:
            summary = run_scheduler_tick()
            for action, draws in summary.items():
                for draw in draws:
                    self.stdout.write(f"  {action:<8} {draw}")
            if ticks % 60 == 0:
                expired = expire_stale_deposits()
                if expired:
                    self.stdout.write(f"  expired  {expired} stale deposit(s)")
            ticks += 1
            if once:
                return
            time.sleep(interval)
