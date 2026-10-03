from decimal import Decimal

from django.conf import settings
from django.db import models

from .money import to_major, to_minor

# Admin field -> rule key. Money fields are edited in naira and used in kobo.
MONEY_RULES = {
    "coin_value": "COIN_VALUE",
    "welcome_bonus": "WELCOME_BONUS",
    "min_deposit": "MIN_DEPOSIT",
    "max_deposit": "MAX_DEPOSIT",
    "tier1_daily_deposit_limit": "TIER1_DAILY_DEPOSIT_LIMIT",
    "default_daily_deposit_limit": "DEFAULT_DAILY_DEPOSIT_LIMIT",
    "tier1_max_stake_per_purchase": "TIER1_MAX_STAKE_PER_PURCHASE",
    "min_withdrawal": "MIN_WITHDRAWAL",
    "withdrawal_review_threshold": "WITHDRAWAL_REVIEW_THRESHOLD",
}
PLAIN_RULES = {
    "aml_wager_multiplier": "AML_WAGER_MULTIPLIER",
    "max_withdrawals_per_day": "MAX_WITHDRAWALS_PER_DAY",
    "new_account_review_hours": "NEW_ACCOUNT_REVIEW_HOURS",
    "limit_increase_cooldown_hours": "LIMIT_INCREASE_COOLDOWN_HOURS",
    "auto_spin_after_minutes": "AUTO_SPIN_AFTER_MINUTES",
    "kyc_require_bank_name_match": "KYC_REQUIRE_BANK_NAME_MATCH",
    "coin_packages": "COIN_PACKAGES",
}


def naira(**kwargs):
    return models.DecimalField(max_digits=14, decimal_places=2, **kwargs)


class PlatformSettings(models.Model):
    """Single row of business rules the admin can change at any time (Admin → Platform settings)."""

    # Coins
    coin_value = naira(default=Decimal("10.00"),
                       help_text="Naira value of 1 coin. Players buy, play, win and cash out in coins at this rate.")
    coin_packages = models.CharField(
        max_length=120, default="100,300,500,1000,2000,5000",
        help_text="Coin packages on the Buy coins page, comma-separated (e.g. 100,300,500,1000).",
    )
    # Promotions
    welcome_bonus = naira(help_text="Bonus credited to every new verified player (₦). Play-only. 0 = off.")
    # Deposits
    min_deposit = naira(verbose_name="Minimum coin purchase (₦)", help_text="Smallest coin purchase, in naira.")
    max_deposit = naira(verbose_name="Maximum coin purchase (₦)", help_text="Largest single coin purchase, in naira.")
    tier1_daily_deposit_limit = naira(help_text="Daily deposit cap before ID verification (₦).")
    default_daily_deposit_limit = naira(help_text="Daily deposit cap for verified players (₦).")
    # Play
    tier1_max_stake_per_purchase = naira(help_text="Most a Tier 1 (unverified ID) player can spend per play (₦).")
    # Withdrawals & AML
    min_withdrawal = naira(help_text="Smallest withdrawal allowed (₦).")
    withdrawal_review_threshold = naira(help_text="Withdrawals at or above this go to manual review (₦).")
    max_withdrawals_per_day = models.PositiveSmallIntegerField(help_text="More than this in 24h goes to review.")
    new_account_review_hours = models.PositiveIntegerField(help_text="Withdrawals from accounts younger than this are reviewed.")
    aml_wager_multiplier = models.DecimalField(
        max_digits=4, decimal_places=2, help_text="Deposits must be played this many times before withdrawal (1 = once)."
    )
    # Responsible gaming
    limit_increase_cooldown_hours = models.PositiveIntegerField(help_text="Delay before a player's raised limit applies.")
    # Games
    auto_spin_after_minutes = models.PositiveIntegerField(
        help_text="Spin closed games automatically after this many minutes if no admin has. 0 = admin only."
    )
    # KYC
    kyc_require_bank_name_match = models.BooleanField(
        help_text="KYC passes only if the name on the ID matches the bank account name."
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "Platform settings"
        verbose_name_plural = "Platform settings"

    def __str__(self):
        return "Platform settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def defaults(cls):
        base = settings.NEXTGEN
        values = {field: to_major(base[key]) for field, key in MONEY_RULES.items()}
        values.update({field: base.get(key, True) for field, key in PLAIN_RULES.items()})
        values["aml_wager_multiplier"] = Decimal(str(base["AML_WAGER_MULTIPLIER"]))
        return values

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults=cls.defaults())
        return obj

    def as_rules(self):
        values = {key: to_minor(getattr(self, field)) for field, key in MONEY_RULES.items()}
        values.update({key: getattr(self, field) for field, key in PLAIN_RULES.items()})
        return values
