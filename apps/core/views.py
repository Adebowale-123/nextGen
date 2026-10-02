from django.db.models import Count, Sum
from django.shortcuts import render

from apps.games import services as games
from apps.games.models import Draw, Ticket


def home(request):
    spin_cards, open_draws = games.lobby()
    context = {"spin_cards": spin_cards, "open_draws": open_draws}
    if request.user.is_authenticated:
        return render(request, "core/dashboard.html", {**context, **dashboard_context(request.user)})
    context["latest_results"] = Draw.objects.filter(status=Draw.Status.SETTLED).select_related("game") \
        .order_by("-closes_at")[:4]
    return render(request, "core/landing.html", context)


def dashboard_context(user):
    tickets = user.tickets.select_related("draw__game")
    won = tickets.filter(status=Ticket.Status.WON)
    stats = tickets.aggregate(played=Count("id"))
    won_stats = won.aggregate(count=Count("id"), total=Sum("prize_amount"))
    return {
        "played_tickets": tickets[:10],
        "won_tickets": won[:10],
        "played_count": stats["played"],
        "won_count": won_stats["count"],
        "won_total": won_stats["total"] or 0,
        "kyc": user.kyc_submissions.select_related("bank_account").first(),
        "bank_account": user.payout_accounts.filter(is_active=True).order_by("-name_verified", "-created_at").first(),
    }


def fairness(request):
    return render(request, "core/fairness.html")
