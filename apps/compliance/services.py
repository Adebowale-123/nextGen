"""Responsible-gaming limits, self-exclusion and risk flagging."""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.core.config import RULES
from apps.core.money import format_coins

from .models import ResponsibleGamingSettings, RiskFlag



class ComplianceError(Exception):
    """Raised when an action is blocked by a compliance rule. Message is user-facing."""


def get_settings(user) -> ResponsibleGamingSettings:
    rg, _ = ResponsibleGamingSettings.objects.get_or_create(user=user)
    _apply_pending_limits(rg)
    return rg


def _apply_pending_limits(rg):
    if rg.pending_effective_at and rg.pending_effective_at <= timezone.now():
        rg.daily_deposit_limit = rg.pending_deposit_limit if rg.pending_deposit_limit is not None else rg.daily_deposit_limit
        rg.daily_stake_limit = rg.pending_stake_limit if rg.pending_stake_limit is not None else rg.daily_stake_limit
        rg.pending_deposit_limit = rg.pending_stake_limit = rg.pending_effective_at = None
        rg.save()


def start_of_today():
    now = timezone.localtime()
    return timezone.make_aware(datetime.combine(now.date(), time.min), now.tzinfo)


def tier_deposit_cap(user):
    from apps.accounts.models import User

    if user.kyc_tier >= User.KycTier.VERIFIED:
        return RULES["DEFAULT_DAILY_DEPOSIT_LIMIT"]
    return RULES["TIER1_DAILY_DEPOSIT_LIMIT"]


def effective_deposit_limit(user):
    rg = get_settings(user)
    cap = tier_deposit_cap(user)
    return min(cap, rg.daily_deposit_limit) if rg.daily_deposit_limit is not None else cap


def deposited_today(user):
    from apps.payments.models import Deposit

    return (
        Deposit.objects.filter(
            user=user,
            created_at__gte=start_of_today(),
            status__in=[Deposit.Status.SUCCESS, Deposit.Status.PENDING],
        ).aggregate(total=Sum("amount"))["total"]
        or 0
    )


def staked_today(user):
    from apps.games.models import Ticket

    return (
        Ticket.objects.filter(user=user, purchased_at__gte=start_of_today())
        .exclude(status=Ticket.Status.REFUNDED)
        .aggregate(total=Sum("stake"))["total"]
        or 0
    )


def ensure_account_active(user):
    if user.status != user.Status.ACTIVE:
        raise ComplianceError("Your account is not active. Please contact support.")


def ensure_not_self_excluded(user):
    rg = get_settings(user)
    if rg.is_self_excluded:
        until = timezone.localtime(rg.self_excluded_until).strftime("%d %b %Y, %H:%M")
        raise ComplianceError(f"You are self-excluded until {until}. Deposits and play are paused.")


def check_can_deposit(user, amount):
    ensure_account_active(user)
    ensure_not_self_excluded(user)
    if not user.is_contact_verified:
        raise ComplianceError("Verify your phone or email before buying coins.")
    if amount < RULES["MIN_DEPOSIT"]:
        raise ComplianceError(f"The minimum purchase is {format_coins(RULES['MIN_DEPOSIT'], with_naira=True)}.")
    if amount > RULES["MAX_DEPOSIT"]:
        raise ComplianceError(f"The maximum single purchase is {format_coins(RULES['MAX_DEPOSIT'], with_naira=True)}.")
    limit = effective_deposit_limit(user)
    used = deposited_today(user)
    if used + amount > limit:
        remaining = max(0, limit - used)
        raise ComplianceError(
            f"This exceeds your daily limit of {format_coins(limit, with_naira=True)}. "
            f"You can buy up to {format_coins(remaining, with_naira=True)} more today."
        )


def check_can_stake(user, amount):
    ensure_account_active(user)
    ensure_not_self_excluded(user)
    if not user.can_play:
        raise ComplianceError("Verify your account to start playing.")
    from apps.accounts.models import User

    if user.kyc_tier < User.KycTier.VERIFIED and amount > RULES["TIER1_MAX_STAKE_PER_PURCHASE"]:
        raise ComplianceError(
            f"Tier 1 accounts can stake up to {format_coins(RULES['TIER1_MAX_STAKE_PER_PURCHASE'])} per purchase. "
            "Complete ID verification to raise this."
        )
    rg = get_settings(user)
    if rg.daily_stake_limit is not None and staked_today(user) + amount > rg.daily_stake_limit:
        raise ComplianceError(f"This exceeds your daily play limit of {format_coins(rg.daily_stake_limit)}.")


def update_limits(user, *, deposit_limit, stake_limit):
    """Decreases apply immediately; increases (or removals) wait for the cooling-off period."""
    rg = get_settings(user)
    now = timezone.now()
    cooldown = timedelta(hours=RULES["LIMIT_INCREASE_COOLDOWN_HOURS"])
    delayed = False

    def is_increase(old, new):
        if old is None:
            return False
        return new is None or new > old

    for field, pending_field, new in (
        ("daily_deposit_limit", "pending_deposit_limit", deposit_limit),
        ("daily_stake_limit", "pending_stake_limit", stake_limit),
    ):
        old = getattr(rg, field)
        if new == old:
            continue
        if is_increase(old, new):
            setattr(rg, pending_field, new)
            delayed = True
        else:
            setattr(rg, field, new)
            setattr(rg, pending_field, None)
    if delayed:
        rg.pending_effective_at = now + cooldown
    rg.save()
    return rg, delayed


def self_exclude(user, days):
    rg = get_settings(user)
    until = timezone.now() + timedelta(days=days)
    # Self-exclusion can be extended but never shortened.
    if not rg.self_excluded_until or until > rg.self_excluded_until:
        rg.self_excluded_until = until
        rg.save(update_fields=["self_excluded_until", "updated_at"])
    return rg


def raise_flag(user, code, detail, severity=RiskFlag.Severity.MEDIUM, source=None, mark_user=False):
    flag = RiskFlag.objects.create(
        user=user,
        code=code,
        detail=detail[:255],
        severity=severity,
        source_type=type(source).__name__ if source is not None else "",
        source_id=str(source.pk) if source is not None else "",
    )
    if mark_user and not user.is_flagged:
        user.is_flagged = True
        user.flag_reason = detail[:255]
        user.save(update_fields=["is_flagged", "flag_reason"])
    return flag
