"""Financial reporting straight from the ledger and operational tables."""

from datetime import date, datetime, time, timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.accounts.models import KycSubmission, User
from apps.compliance.models import RiskFlag
from apps.games.models import Draw, Ticket
from apps.ledger.models import LedgerAccount
from apps.payments.models import Deposit, Withdrawal


def day_bounds(start: date, end: date):
    tz = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(start, time.min), tz),
        timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz),
    )


def period_summary(start: date, end: date, currency="NGN"):
    lo, hi = day_bounds(start, end)
    deposits = Deposit.objects.filter(status=Deposit.Status.SUCCESS, completed_at__gte=lo, completed_at__lt=hi,
                                      currency=currency).aggregate(total=Sum("amount"), n=Count("id"))
    withdrawals = Withdrawal.objects.filter(status=Withdrawal.Status.PAID, completed_at__gte=lo, completed_at__lt=hi,
                                            currency=currency).aggregate(total=Sum("amount"), n=Count("id"))
    tickets = Ticket.objects.filter(purchased_at__gte=lo, purchased_at__lt=hi, draw__game__currency=currency) \
        .exclude(status=Ticket.Status.REFUNDED).aggregate(stake=Sum("stake"), n=Count("id"))
    prizes = Draw.objects.filter(settled_at__gte=lo, settled_at__lt=hi, game__currency=currency) \
        .aggregate(prizes=Sum("total_prizes"), stake=Sum("total_stake"), n=Count("id"))
    settled_stake = prizes["stake"] or 0
    settled_prizes = prizes["prizes"] or 0
    return {
        "deposits": deposits["total"] or 0,
        "deposit_count": deposits["n"],
        "withdrawals": withdrawals["total"] or 0,
        "withdrawal_count": withdrawals["n"],
        "stakes": tickets["stake"] or 0,
        "ticket_count": tickets["n"],
        "draws_settled": prizes["n"],
        "settled_stake": settled_stake,
        "prizes": settled_prizes,
        "ggr": settled_stake - settled_prizes,  # gross gaming revenue on settled draws
        "payout_ratio": round(settled_prizes * 100 / settled_stake, 1) if settled_stake else None,
        "new_players": User.objects.filter(date_joined__gte=lo, date_joined__lt=hi, is_staff=False).count(),
    }


def daily_breakdown(start: date, end: date, currency="NGN"):
    lo, hi = day_bounds(start, end)
    rows = {start + timedelta(days=i): {"deposits": 0, "withdrawals": 0, "stakes": 0, "prizes": 0}
            for i in range((end - start).days + 1)}

    def fill(qs, date_field, amount_field, key):
        for row in qs.annotate(day=TruncDate(date_field)).values("day").annotate(total=Sum(amount_field)):
            if row["day"] in rows:
                rows[row["day"]][key] = row["total"] or 0

    fill(Deposit.objects.filter(status=Deposit.Status.SUCCESS, completed_at__gte=lo, completed_at__lt=hi,
                                currency=currency), "completed_at", "amount", "deposits")
    fill(Withdrawal.objects.filter(status=Withdrawal.Status.PAID, completed_at__gte=lo, completed_at__lt=hi,
                                   currency=currency), "completed_at", "amount", "withdrawals")
    fill(Ticket.objects.filter(purchased_at__gte=lo, purchased_at__lt=hi, draw__game__currency=currency)
         .exclude(status=Ticket.Status.REFUNDED), "purchased_at", "stake", "stakes")
    fill(Draw.objects.filter(settled_at__gte=lo, settled_at__lt=hi, game__currency=currency),
         "settled_at", "total_prizes", "prizes")
    return [{"day": day, **values, "net": values["stakes"] - values["prizes"]} for day, values in sorted(rows.items())]


def segregation_summary(currency="NGN"):
    """Player funds (owed to players) vs operator funds, from ledger balances."""
    balances = dict(
        LedgerAccount.objects.filter(currency=currency).values("purpose").annotate(total=Sum("balance"))
        .values_list("purpose", "total")
    )
    player_liability = sum(balances.get(p, 0) for p in LedgerAccount.PLAYER_FUND_PURPOSES)
    clearing = balances.get(LedgerAccount.Purpose.PROVIDER_CLEARING, 0)
    return {
        "player_cash": balances.get(LedgerAccount.Purpose.PLAYER_CASH, 0),
        "pool_hold": balances.get(LedgerAccount.Purpose.POOL_HOLD, 0),
        "withdrawal_pending": balances.get(LedgerAccount.Purpose.WITHDRAWAL_PENDING, 0),
        "player_liability": player_liability,
        "provider_clearing": clearing,
        "house_revenue": balances.get(LedgerAccount.Purpose.HOUSE_REVENUE, 0),
        # Assets held at providers must cover what is owed to players.
        "coverage_ok": clearing >= player_liability,
    }


def ops_queues():
    return {
        "spins_pending": Draw.objects.filter(status=Draw.Status.LOCKED, game__mode="spin").count(),
        "withdrawals_review": Withdrawal.objects.filter(status=Withdrawal.Status.PENDING_REVIEW).count(),
        "kyc_pending": KycSubmission.objects.filter(status=KycSubmission.Status.PENDING).count(),
        "open_flags": RiskFlag.objects.filter(resolved=False).count(),
        "flagged_users": User.objects.filter(is_flagged=True).count(),
        "stuck_withdrawals": Withdrawal.objects.filter(
            status=Withdrawal.Status.PROCESSING, created_at__lt=timezone.now() - timedelta(hours=1)
        ).count(),
    }


def player_counts():
    return User.objects.filter(is_staff=False).aggregate(
        total=Count("id"),
        tier1=Count("id", filter=Q(kyc_tier=User.KycTier.BASIC)),
        tier2=Count("id", filter=Q(kyc_tier=User.KycTier.VERIFIED)),
    )
