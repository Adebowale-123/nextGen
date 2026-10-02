from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce

from .models import Entry, JournalTransaction, LedgerAccount, Wallet


class LedgerError(Exception):
    pass


class InsufficientFunds(LedgerError):
    pass


@dataclass(frozen=True)
class Leg:
    account: LedgerAccount
    direction: str  # Entry.Direction.DEBIT / CREDIT
    amount: int


def debit(account, amount):
    return Leg(account, Entry.Direction.DEBIT, amount)


def credit(account, amount):
    return Leg(account, Entry.Direction.CREDIT, amount)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

SYSTEM_ACCOUNT_KINDS = {
    LedgerAccount.Purpose.POOL_HOLD: LedgerAccount.Kind.LIABILITY,
    LedgerAccount.Purpose.PRIZE_CARRYOVER: LedgerAccount.Kind.LIABILITY,
    LedgerAccount.Purpose.WITHDRAWAL_PENDING: LedgerAccount.Kind.LIABILITY,
    LedgerAccount.Purpose.PROVIDER_CLEARING: LedgerAccount.Kind.ASSET,
    LedgerAccount.Purpose.HOUSE_REVENUE: LedgerAccount.Kind.REVENUE,
}


def get_system_account(purpose, currency, qualifier=""):
    """Fetch or create an operator-side account, e.g. provider clearing for Paystack."""
    code = f"system:{purpose}:{qualifier + ':' if qualifier else ''}{currency}"
    label = LedgerAccount.Purpose(purpose).label + (f" ({qualifier})" if qualifier else "") + f" {currency}"
    account, _ = LedgerAccount.objects.get_or_create(
        code=code,
        defaults={"name": label, "kind": SYSTEM_ACCOUNT_KINDS[purpose], "purpose": purpose, "currency": currency},
    )
    return account


def create_wallet(user, currency) -> Wallet:
    """Initialise a player's wallet at 0.00 (idempotent)."""
    existing = Wallet.objects.filter(user=user, currency=currency).first()
    if existing:
        return existing
    with transaction.atomic():
        account = LedgerAccount.objects.create(
            code=f"player:{user.public_id}:{currency}",
            name=f"Player cash · {user} · {currency}",
            kind=LedgerAccount.Kind.LIABILITY,
            purpose=LedgerAccount.Purpose.PLAYER_CASH,
            currency=currency,
            user=user,
        )
        wallet = Wallet.objects.create(user=user, currency=currency, account=account)
        ensure_bonus_account(wallet)
        return wallet


def ensure_bonus_account(wallet) -> LedgerAccount:
    """Each wallet has a play-only bonus sub-account (created lazily for older wallets)."""
    if wallet.bonus_account_id:
        return wallet.bonus_account
    account, _ = LedgerAccount.objects.get_or_create(
        code=f"player-bonus:{wallet.user.public_id}:{wallet.currency}",
        defaults={
            "name": f"Player bonus · {wallet.user} · {wallet.currency}",
            "kind": LedgerAccount.Kind.LIABILITY,
            "purpose": LedgerAccount.Purpose.PLAYER_BONUS,
            "currency": wallet.currency,
            "user": wallet.user,
        },
    )
    Wallet.objects.filter(pk=wallet.pk).update(bonus_account=account)
    wallet.bonus_account = account
    return account


def get_wallet(user, currency) -> Wallet:
    """The user's wallet, created on first use (e.g. for admin accounts made outside sign-up)."""
    wallet = Wallet.objects.select_related("account", "bonus_account").filter(user=user, currency=currency).first()
    return wallet or create_wallet(user, currency)


def lock_wallet(user, currency) -> Wallet:
    """Row-lock a wallet (and its ledger account) for the current transaction.

    This is the double-spend guard: concurrent purchases/withdrawals for the
    same wallet serialise on this lock. Must be called inside atomic().
    """
    get_wallet(user, currency)  # make sure it exists before locking
    wallet = Wallet.objects.select_for_update().select_related("account").get(user=user, currency=currency)
    # Lock the account row too and refresh the balance we read.
    wallet.account = LedgerAccount.objects.select_for_update().get(pk=wallet.account_id)
    return wallet


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def post_transaction(
    tx_type,
    legs,
    *,
    description="",
    idempotency_key=None,
    source=None,
    metadata=None,
    created_by=None,
) -> JournalTransaction:
    """Atomically post a balanced journal transaction.

    - Debits must equal credits, amounts must be positive integers, and all
      accounts must share one currency.
    - Accounts are locked in primary-key order to avoid deadlocks.
    - A player cash account can never go negative (InsufficientFunds).
    - With an idempotency_key, re-posting returns the original transaction.
    """
    legs = list(legs)
    if len(legs) < 2:
        raise LedgerError("A transaction needs at least two legs.")
    for leg in legs:
        if not isinstance(leg.amount, int) or leg.amount <= 0:
            raise LedgerError(f"Invalid leg amount: {leg.amount!r}")
    debits = sum(leg.amount for leg in legs if leg.direction == Entry.Direction.DEBIT)
    credits = sum(leg.amount for leg in legs if leg.direction == Entry.Direction.CREDIT)
    if debits != credits:
        raise LedgerError(f"Unbalanced transaction: debits {debits} != credits {credits}")
    currencies = {leg.account.currency for leg in legs}
    if len(currencies) != 1:
        raise LedgerError(f"Mixed currencies in one transaction: {currencies}")

    if idempotency_key:
        existing = JournalTransaction.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

    try:
        with transaction.atomic():
            ids = sorted({leg.account.pk for leg in legs})
            locked = {a.pk: a for a in LedgerAccount.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
            txn = JournalTransaction.objects.create(
                tx_type=tx_type,
                currency=currencies.pop(),
                description=description[:255],
                idempotency_key=idempotency_key,
                source_type=type(source).__name__ if source is not None else "",
                source_id=str(source.pk) if source is not None else "",
                metadata=metadata or {},
                created_by=created_by,
            )
            for leg in legs:
                account = locked[leg.account.pk]
                increases = (leg.direction == Entry.Direction.DEBIT) == account.is_debit_normal
                account.balance += leg.amount if increases else -leg.amount
                if account.purpose in (LedgerAccount.Purpose.PLAYER_CASH, LedgerAccount.Purpose.PLAYER_BONUS) \
                        and account.balance < 0:
                    raise InsufficientFunds("Insufficient wallet balance.")
                account.save(update_fields=["balance"])
                Entry.objects.create(
                    transaction=txn,
                    account=account,
                    direction=leg.direction,
                    amount=leg.amount,
                    balance_after=account.balance,
                )
                # Keep caller-held instances in sync with the database.
                leg.account.balance = account.balance
            return txn
    except IntegrityError:
        if idempotency_key:
            existing = JournalTransaction.objects.filter(idempotency_key=idempotency_key).first()
            if existing:
                return existing
        raise


# ---------------------------------------------------------------------------
# Integrity checks
# ---------------------------------------------------------------------------


def verify_ledger_integrity():
    """Return a list of problems (empty when the books are consistent)."""
    problems = []
    is_debit = Q(entries__direction=Entry.Direction.DEBIT)
    is_credit = Q(entries__direction=Entry.Direction.CREDIT)
    unbalanced = (
        JournalTransaction.objects.annotate(
            d=Coalesce(Sum("entries__amount", filter=is_debit), 0),
            c=Coalesce(Sum("entries__amount", filter=is_credit), 0),
        )
        .exclude(d=F("c"))
        .values_list("reference", flat=True)
    )
    problems += [f"Unbalanced transaction {ref}" for ref in unbalanced]

    for account in LedgerAccount.objects.annotate(
        d=Coalesce(Sum("entries__amount", filter=is_debit), 0),
        c=Coalesce(Sum("entries__amount", filter=is_credit), 0),
    ):
        expected = account.d - account.c if account.is_debit_normal else account.c - account.d
        if expected != account.balance:
            problems.append(f"Account {account.code}: stored balance {account.balance} != entries {expected}")
    return problems
