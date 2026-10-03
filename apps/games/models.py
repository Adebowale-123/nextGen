import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def default_schedule():
    return [["08:00", "17:00"], ["20:00", "24:00"]]


class Game(models.Model):
    class Mode(models.TextChoices):
        SPIN = "spin", "Spin game (numbers given automatically, admin spins)"
        PICK = "pick", "Pick-your-numbers daily draw"

    name = models.CharField(max_length=80)
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.SPIN)
    slug = models.SlugField(unique=True)
    tagline = models.CharField(max_length=160, blank=True)
    currency = models.CharField(max_length=3, default="NGN")
    ticket_price = models.BigIntegerField(help_text="Minor units (kobo).")
    pick_count = models.PositiveSmallIntegerField(help_text="How many numbers a player picks (N).")
    number_max = models.PositiveSmallIntegerField(help_text="Numbers are drawn from 1..M.")
    draw_time = models.TimeField(
        null=True, blank=True, help_text="Pick games: daily draw time (Africa/Lagos). Sales close at this time."
    )
    # --- Spin games ---
    schedule = models.JSONField(
        default=default_schedule, blank=True,
        help_text='Spin games: daily play windows (Africa/Lagos), e.g. [["08:00","17:00"],["20:00","24:00"]].',
    )
    prize_pool_percent = models.PositiveSmallIntegerField(default=60, verbose_name="Prize share of sales (%)", help_text="Share of each batch's sales paid out as prizes. The rest is company profit.")
    grand_prize = models.BigIntegerField(default=100_000_00, help_text="Minor units. Paid for matching all numbers.")
    consolation_prize = models.BigIntegerField(default=7_000_00, help_text="Minor units.")
    max_lines_per_purchase = models.PositiveSmallIntegerField(default=10)
    max_tickets_per_draw = models.PositiveIntegerField(default=100, help_text="Per player, per draw.")
    is_active = models.BooleanField(default=True)
    accent = models.CharField(max_length=20, default="violet", help_text="UI colour theme key.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["draw_time"]

    def __str__(self):
        return self.name

    @property
    def format_label(self):
        return f"{self.pick_count}/{self.number_max}"

    @property
    def is_spin(self):
        return self.mode == self.Mode.SPIN


class PrizeTier(models.Model):
    class PayoutType(models.TextChoices):
        FIXED_MULTIPLIER = "fixed", "Fixed multiple of stake"
        POOL_SHARE = "pool", "Share of draw pool (%), split between winners"

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="prize_tiers")
    name = models.CharField(max_length=40)
    match_count = models.PositiveSmallIntegerField()
    payout_type = models.CharField(max_length=8, choices=PayoutType.choices)
    value = models.DecimalField(max_digits=10, decimal_places=2, help_text="Multiplier (e.g. 10) or pool % (e.g. 25)")

    class Meta:
        ordering = ["-match_count"]
        constraints = [models.UniqueConstraint(fields=["game", "match_count"], name="one_tier_per_match_count")]

    def __str__(self):
        return f"{self.game} · {self.name} (match {self.match_count})"

    @property
    def payout_label(self):
        if self.payout_type == self.PayoutType.FIXED_MULTIPLIER:
            return f"{self.value.normalize():f}× stake"
        return f"{self.value.normalize():f}% of pool"


def new_server_seed():
    return secrets.token_hex(32)


class Draw(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open for sales"
        LOCKED = "locked", "Closed — awaiting spin"
        DRAWN = "drawn", "Numbers drawn"
        SETTLED = "settled", "Settled"
        CANCELLED = "cancelled", "Cancelled"

    game = models.ForeignKey(Game, on_delete=models.PROTECT, related_name="draws")
    draw_number = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    opens_at = models.DateTimeField(default=timezone.now)
    closes_at = models.DateTimeField()
    # Provably fair: the hash is published when the draw opens; the seed is
    # revealed only after the numbers are drawn.
    server_seed = models.CharField(max_length=64, default=new_server_seed, editable=False)
    server_seed_hash = models.CharField(max_length=64, editable=False)
    client_seed = models.CharField(max_length=64, blank=True, editable=False)
    winning_numbers = models.JSONField(default=list, blank=True)
    ticket_count = models.PositiveIntegerField(default=0)
    total_stake = models.BigIntegerField(default=0)
    total_prizes = models.BigIntegerField(default=0)
    winner_count = models.PositiveIntegerField(default=0)
    # Spin-game settlement figures
    prize_pool = models.BigIntegerField(default=0)
    house_share = models.BigIntegerField(default=0)
    carry_in = models.BigIntegerField(default=0)
    carry_out = models.BigIntegerField(default=0)
    house_topup = models.BigIntegerField(default=0)
    grand_winner_count = models.PositiveIntegerField(default=0)
    consolation_winner_count = models.PositiveIntegerField(default=0)
    spun_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    locked_at = models.DateTimeField(null=True, blank=True)
    drawn_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["closes_at"]
        verbose_name = "Game round"
        constraints = [models.UniqueConstraint(fields=["game", "draw_number"], name="unique_draw_number")]
        indexes = [models.Index(fields=["status", "closes_at"])]

    def __str__(self):
        return f"{self.game.name} #{self.draw_number}"

    def save(self, *args, **kwargs):
        if not self.server_seed_hash:
            from .rng import sha256_hex

            self.server_seed_hash = sha256_hex(self.server_seed)
        super().save(*args, **kwargs)

    @property
    def nonce(self):
        return f"{self.game.slug}-{self.draw_number}"

    @property
    def is_selling(self):
        return self.status == self.Status.OPEN and timezone.now() < self.closes_at

    @property
    def seed_revealed(self):
        return self.status in (self.Status.DRAWN, self.Status.SETTLED)

    @property
    def public_server_seed(self):
        return self.server_seed if self.seed_revealed else None


def ticket_serial():
    return "NG-" + secrets.token_hex(6).upper()


class Ticket(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Awaiting draw"
        WON = "won", "Won"
        LOST = "lost", "No win"
        REFUNDED = "refunded", "Refunded"

    serial = models.CharField(max_length=20, unique=True, default=ticket_serial)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tickets")
    draw = models.ForeignKey(Draw, on_delete=models.PROTECT, related_name="tickets")
    numbers = models.JSONField()
    is_quick_pick = models.BooleanField(default=False)
    stake = models.BigIntegerField()
    bonus_stake = models.BigIntegerField(default=0, help_text="Part of the stake paid from bonus credit.")
    signature = models.CharField(max_length=64, unique=True)
    purchased_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    match_count = models.PositiveSmallIntegerField(null=True, blank=True)
    prize_tier = models.ForeignKey(PrizeTier, null=True, blank=True, on_delete=models.PROTECT)
    prize_amount = models.BigIntegerField(default=0)
    prize_label = models.CharField(max_length=40, blank=True)
    purchase_transaction = models.ForeignKey("ledger.JournalTransaction", on_delete=models.PROTECT, related_name="+")
    payout_transaction = models.ForeignKey(
        "ledger.JournalTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-purchased_at", "-id"]
        indexes = [models.Index(fields=["draw", "status"]), models.Index(fields=["user", "-purchased_at"])]

    def __str__(self):
        return self.serial
