from datetime import time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.games.models import Game, PrizeTier
from apps.games.services import open_next_draw, run_scheduler_tick

SPIN_GAMES = [
    {
        "slug": "nextgen-daily",
        "name": "NextGen Daily",
        "mode": Game.Mode.SPIN,
        "tagline": "One tap, 4 numbers. Grand prize ₦100,000.",
        "ticket_price": 500_00,
        "pick_count": 4,
        "number_max": 90,
        "schedule": [["08:00", "17:00"], ["20:00", "24:00"]],
        "prize_pool_percent": 60,
        "grand_prize": 100_000_00,
        "consolation_prize": 7_000_00,
        "consolation_min_match": 1,
        "max_lines_per_purchase": 1,
        "max_tickets_per_draw": 50,
        "is_active": True,
    },
]

# The original pick-your-numbers games stay in the code but are switched off.
PICK_GAMES = [
    {
        "slug": "spark-5-30", "name": "Spark 5/30", "mode": Game.Mode.PICK, "tagline": "Pick 5 from 30.",
        "ticket_price": 100_00, "pick_count": 5, "number_max": 30, "draw_time": time(20, 0), "is_active": False,
        "tiers": [("Jackpot", 5, PrizeTier.PayoutType.POOL_SHARE, Decimal("25")),
                  ("Match 4", 4, PrizeTier.PayoutType.FIXED_MULTIPLIER, Decimal("200")),
                  ("Match 3", 3, PrizeTier.PayoutType.FIXED_MULTIPLIER, Decimal("10")),
                  ("Match 2", 2, PrizeTier.PayoutType.FIXED_MULTIPLIER, Decimal("1"))],
    },
    {
        "slug": "midday-flash-4-20", "name": "Midday Flash 4/20", "mode": Game.Mode.PICK, "tagline": "Pick 4 from 20.",
        "ticket_price": 50_00, "pick_count": 4, "number_max": 20, "draw_time": time(13, 0), "is_active": False,
        "tiers": [("Top prize", 4, PrizeTier.PayoutType.FIXED_MULTIPLIER, Decimal("1000")),
                  ("Match 3", 3, PrizeTier.PayoutType.FIXED_MULTIPLIER, Decimal("20")),
                  ("Match 2", 2, PrizeTier.PayoutType.FIXED_MULTIPLIER, Decimal("1"))],
    },
]


class Command(BaseCommand):
    help = "Create/refresh the games and open any batch that should be open now."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quick-batch", type=int, metavar="MINUTES",
            help="Also open an extra NextGen Daily batch that closes in MINUTES (for testing the spin).",
        )

    def handle(self, *args, quick_batch=None, **options):
        for spec in SPIN_GAMES:
            game, created = Game.objects.update_or_create(slug=spec["slug"], defaults=spec)
            self.stdout.write(f"{'Created' if created else 'Updated'} {game} (spin game, NGN {game.ticket_price // 100})")
        for spec in PICK_GAMES:
            spec = dict(spec)
            tiers = spec.pop("tiers")
            game, _ = Game.objects.update_or_create(slug=spec["slug"], defaults=spec)
            for name, match, payout_type, value in tiers:
                PrizeTier.objects.update_or_create(
                    game=game, match_count=match, defaults={"name": name, "payout_type": payout_type, "value": value}
                )
        summary = run_scheduler_tick()
        for draw in summary.get("opened", []):
            self.stdout.write(f"Opened {draw}")
        if quick_batch:
            game = Game.objects.get(slug="nextgen-daily")
            draw = open_next_draw(game, closes_at=timezone.now() + timedelta(minutes=quick_batch))
            self.stdout.write(self.style.SUCCESS(f"Quick test batch {draw} closes in {quick_batch} min."))
