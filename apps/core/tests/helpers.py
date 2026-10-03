from datetime import date, time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import register_user
from apps.games.models import Game, PrizeTier
from apps.games.services import open_next_draw
from apps.ledger.models import JournalTransaction, LedgerAccount
from apps.ledger.services import credit, debit, get_system_account, post_transaction

_counter = {"n": 0}


def make_user(*, tier=User.KycTier.BASIC, balance=0, wagering=0, email=None, aged_hours=48, **extra):
    _counter["n"] += 1
    n = _counter["n"]
    user = register_user(
        email=email or f"player{n}@example.com",
        password="Sup3r-secret-pass",
        first_name="Ada",
        last_name=f"Obi{n}",
        date_of_birth=date(1990, 1, 1),
        email_verified=tier >= User.KycTier.BASIC,
    )
    user.kyc_tier = tier
    user.date_joined = timezone.now() - timedelta(hours=aged_hours)
    for key, value in extra.items():
        setattr(user, key, value)
    user.save()
    if balance:
        fund(user, balance, wagering=wagering)
    return user


def fund(user, amount, wagering=0, currency="NGN"):
    """Give a player bought coins (as a settled coin purchase would)."""
    from apps.ledger.services import ensure_coin_account

    wallet = user.wallets.get(currency=currency)
    clearing = get_system_account(LedgerAccount.Purpose.PROVIDER_CLEARING, currency, "mock")
    post_transaction(JournalTransaction.Type.DEPOSIT,
                     [debit(clearing, amount), credit(ensure_coin_account(wallet), amount)])
    wallet.refresh_from_db()
    wallet.total_deposited += amount
    wallet.save()
    return wallet


def fund_winnings(user, amount, currency="NGN"):
    """Give a player withdrawable prize money (as a won game would)."""
    wallet = user.wallets.get(currency=currency)
    house = get_system_account(LedgerAccount.Purpose.HOUSE_REVENUE, currency)
    post_transaction(JournalTransaction.Type.PRIZE_PAYOUT, [debit(house, amount), credit(wallet.account, amount)])
    return wallet


def balance(user, currency="NGN"):
    """Withdrawable winnings (kobo)."""
    return user.wallets.select_related("account").get(currency=currency).account.balance


def coins(user, currency="NGN"):
    """Coins available to play (bought + bonus), in kobo."""
    return user.wallets.select_related("coin_account", "bonus_account").get(currency=currency).coin_balance


def make_game(**overrides):
    _counter["n"] += 1
    spec = dict(
        name=f"Test 5/30 #{_counter['n']}", slug=f"test-{_counter['n']}", ticket_price=100_00, pick_count=5,
        number_max=30, draw_time=time(20, 0), mode=Game.Mode.PICK,
    )
    spec.update(overrides)
    game = Game.objects.create(**spec)
    PrizeTier.objects.create(game=game, name="Jackpot", match_count=5, payout_type="pool", value=Decimal("50"))
    PrizeTier.objects.create(game=game, name="Match 4", match_count=4, payout_type="fixed", value=Decimal("200"))
    PrizeTier.objects.create(game=game, name="Match 3", match_count=3, payout_type="fixed", value=Decimal("10"))
    return game


def make_open_draw(game=None, minutes=30):
    game = game or make_game()
    return open_next_draw(game, closes_at=timezone.now() + timedelta(minutes=minutes))
