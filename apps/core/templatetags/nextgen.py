from django import template

from apps.core.money import format_money, to_major

register = template.Library()


@register.filter
def money(minor, currency="NGN"):
    return format_money(minor, currency or "NGN")


@register.filter
def contains(collection, item):
    return item in (collection or [])


@register.filter
def percent_of(part, whole):
    if not whole:
        return 0
    return max(0, min(100, round(part * 100 / whole)))


@register.filter
def major(minor):
    """Minor units -> plain major-unit string for form values (e.g. 50000 -> "500.00")."""
    return f"{to_major(minor):.2f}"


@register.simple_tag
def new_token():
    """Fresh idempotency token for a purchase form (stops double-click double charges)."""
    import uuid

    return uuid.uuid4().hex
