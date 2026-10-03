"""Money helpers. All amounts are stored as integers in minor units (kobo)."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CURRENCY_SYMBOLS = {"NGN": "₦", "KES": "KSh ", "GHS": "GH₵", "USD": "$"}


def to_minor(amount) -> int:
    """Convert a major-unit amount (e.g. "1500.50") to minor units (150050)."""
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid amount: {amount!r}") from exc
    return int(value * 100)


def to_major(minor: int) -> Decimal:
    return (Decimal(minor) / 100).quantize(Decimal("0.01"))


def format_money(minor, currency="NGN") -> str:
    if minor is None or minor == "":
        return "—"
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    sign = "-" if minor < 0 else ""
    return f"{sign}{symbol}{abs(to_major(minor)):,.2f}"


# ---------------------------------------------------------------------------
# Coins: what players see. Money is still stored in kobo; 1 coin = COIN_VALUE kobo
# (₦10 by default, changeable in Admin → Platform settings).
# ---------------------------------------------------------------------------


def coin_value() -> int:
    from .config import RULES

    return RULES["COIN_VALUE"]


def coins_to_minor(coins) -> int:
    return int(Decimal(str(coins)) * coin_value())


def to_coins(minor) -> Decimal:
    return Decimal(minor) / coin_value()


def format_coins(minor, with_naira=False) -> str:
    if minor is None or minor == "":
        return "—"
    coins = to_coins(minor)
    number = f"{coins:,.0f}" if coins == coins.to_integral_value() else f"{coins:,.1f}"
    text = f"{number} coin" + ("" if abs(coins) == 1 else "s")
    if with_naira:
        text += f" ({format_money(abs(minor))})"
    return text
