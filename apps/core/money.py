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
    if minor is None:
        return "—"
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    sign = "-" if minor < 0 else ""
    return f"{sign}{symbol}{abs(to_major(minor)):,.2f}"
