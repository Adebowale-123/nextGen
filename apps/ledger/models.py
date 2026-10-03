"""
Double-entry ledger.

Every money movement is a JournalTransaction with two or more Entries whose
debits equal their credits. Entries are append-only: the application refuses
to update or delete them, and on PostgreSQL a database trigger enforces the
same rule (see migration 0002).

Amounts are integers in minor units. LedgerAccount.balance is kept in the
account's *normal* direction: debit-normal for assets, credit-normal for
liabilities and revenue. A player's wallet is a liability of the operator,
so a credit increases what the player holds.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class ImmutableRecordError(Exception):
    pass


class LedgerAccount(models.Model):
    class Kind(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        REVENUE = "revenue", "Revenue"
        EXPENSE = "expense", "Expense"
        EQUITY = "equity", "Equity"

    class Purpose(models.TextChoices):
        # Player funds (segregated from operator funds)
        PLAYER_CASH = "player_cash", "Player winnings (withdrawable)"
        PLAYER_COINS = "player_coins", "Player coins (bought, play-only)"
        PLAYER_BONUS = "player_bonus", "Player bonus coins (play-only)"
        POOL_HOLD = "pool_hold", "Draw pool hold"
        PRIZE_CARRYOVER = "prize_carryover", "Prize money carried to next batch"
        WITHDRAWAL_PENDING = "withdrawal_pending", "Withdrawals in flight"
        # Operator / external
        PROVIDER_CLEARING = "provider_clearing", "Payment provider clearing"
        HOUSE_REVENUE = "house_revenue", "House gaming revenue"

    PLAYER_FUND_PURPOSES = (Purpose.PLAYER_CASH, Purpose.PLAYER_COINS, Purpose.POOL_HOLD,
                            Purpose.WITHDRAWAL_PENDING, Purpose.PRIZE_CARRYOVER)

    code = models.CharField(max_length=120, unique=True)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    purpose = models.CharField(max_length=24, choices=Purpose.choices)
    currency = models.CharField(max_length=3)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="ledger_accounts"
    )
    balance = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(purpose__in=["player_cash", "player_coins", "player_bonus"]) | Q(balance__gte=0),
                name="player_cash_non_negative",
            ),
        ]

    def __str__(self):
        return self.code

    @property
    def is_debit_normal(self):
        return self.kind in (self.Kind.ASSET, self.Kind.EXPENSE)


class Wallet(models.Model):
    """A player's wallet in one currency, backed by a PLAYER_CASH ledger account."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="wallets")
    currency = models.CharField(max_length=3)
    account = models.OneToOneField(LedgerAccount, on_delete=models.PROTECT, related_name="wallet")
    # Play-only bonus credit (e.g. welcome bonus). Spent before cash; never withdrawable.
    # Coins the player bought. Spent on games; never withdrawable.
    coin_account = models.OneToOneField(
        LedgerAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="coin_wallet"
    )
    bonus_account = models.OneToOneField(
        LedgerAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="bonus_wallet"
    )
    # AML play-through: amount that must still be staked before a withdrawal.
    wagering_remaining = models.BigIntegerField(default=0)
    total_deposited = models.BigIntegerField(default=0)
    total_withdrawn = models.BigIntegerField(default=0)
    total_staked = models.BigIntegerField(default=0)
    total_won = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "currency"], name="one_wallet_per_currency")]

    def __str__(self):
        return f"{self.user} · {self.currency}"

    @property
    def balance(self):
        return self.account.balance

    @property
    def bonus_balance(self):
        return self.bonus_account.balance if self.bonus_account_id else 0

    @property
    def coin_balance(self):
        """Coins available to play with (bought + bonus), in kobo."""
        bought = self.coin_account.balance if self.coin_account_id else 0
        return bought + self.bonus_balance

    @property
    def winnings_balance(self):
        """Prize money the player can withdraw, in kobo."""
        return self.account.balance


class JournalTransaction(models.Model):
    class Type(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        TICKET_PURCHASE = "ticket_purchase", "Ticket purchase"
        PRIZE_PAYOUT = "prize_payout", "Prize payout"
        POOL_TOPUP = "pool_topup", "House top-up of prize pool"
        POOL_SETTLEMENT = "pool_settlement", "Pool settlement to house"
        TICKET_REFUND = "ticket_refund", "Ticket refund"
        WITHDRAWAL_HOLD = "withdrawal_hold", "Withdrawal hold"
        WITHDRAWAL_PAID = "withdrawal_paid", "Withdrawal paid out"
        WITHDRAWAL_REVERSAL = "withdrawal_reversal", "Withdrawal reversed"
        WELCOME_BONUS = "welcome_bonus", "Welcome bonus"
        PRIZE_CARRYOVER = "prize_carryover", "Prize money carried over"

    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    idempotency_key = models.CharField(max_length=120, unique=True, null=True, blank=True)
    tx_type = models.CharField(max_length=24, choices=Type.choices)
    currency = models.CharField(max_length=3)
    description = models.CharField(max_length=255, blank=True)
    source_type = models.CharField(max_length=40, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["source_type", "source_id"])]

    def __str__(self):
        return f"{self.get_tx_type_display()} {self.reference}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError("Journal transactions are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError("Journal transactions cannot be deleted.")


class Entry(models.Model):
    class Direction(models.TextChoices):
        DEBIT = "D", "Debit"
        CREDIT = "C", "Credit"

    transaction = models.ForeignKey(JournalTransaction, on_delete=models.PROTECT, related_name="entries")
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name="entries")
    direction = models.CharField(max_length=1, choices=Direction.choices)
    amount = models.BigIntegerField()
    balance_after = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="entry_amount_positive")]
        indexes = [models.Index(fields=["account", "-created_at"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError("Ledger entries are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError("Ledger entries cannot be deleted.")

    @property
    def signed_amount(self):
        """Effect on the account balance (positive = balance went up)."""
        increases = (self.direction == self.Direction.DEBIT) == self.account.is_debit_normal
        return self.amount if increases else -self.amount
