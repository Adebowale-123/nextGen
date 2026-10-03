import csv
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.money import format_money, to_major
from apps.games import services as games
from apps.games.models import Draw
from apps.games.services import run_scheduler_tick
from apps.ledger.models import LedgerAccount
from apps.ledger.services import verify_ledger_integrity

from . import reports


def _parse_range(request, default_days=7):
    today = timezone.localdate()
    try:
        end = date.fromisoformat(request.GET.get("end", ""))
    except ValueError:
        end = today
    try:
        start = date.fromisoformat(request.GET.get("start", ""))
    except ValueError:
        start = end - timedelta(days=default_days - 1)
    if start > end:
        start, end = end, start
    return start, min(end, start + timedelta(days=366))


@staff_member_required
def dashboard(request):
    today = timezone.localdate()
    return render(request, "backoffice/dashboard.html", {
        "today": reports.period_summary(today, today),
        "week": reports.period_summary(today - timedelta(days=6), today),
        "queues": reports.ops_queues(),
        "segregation": reports.segregation_summary(),
        "players": reports.player_counts(),
        "upcoming": Draw.objects.filter(status=Draw.Status.OPEN).select_related("game").order_by("closes_at")[:6],
        "recent_draws": Draw.objects.exclude(status=Draw.Status.OPEN).select_related("game")
                        .order_by("-closes_at")[:6],
    })


@staff_member_required
def financial_report(request):
    start, end = _parse_range(request, default_days=30)
    rows = reports.daily_breakdown(start, end)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="nextgen-report-{start}-{end}.csv"'
        writer = csv.writer(response)
        writer.writerow(["date", "deposits", "withdrawals", "stakes", "prizes", "net_gaming_revenue"])
        for r in rows:
            writer.writerow([r["day"], to_major(r["deposits"]), to_major(r["withdrawals"]), to_major(r["stakes"]),
                             to_major(r["prizes"]), to_major(r["net"])])
        return response
    return render(request, "backoffice/report.html", {
        "start": start, "end": end, "rows": rows, "summary": reports.period_summary(start, end),
    })


@staff_member_required
def ledger_overview(request):
    system_accounts = LedgerAccount.objects.filter(user__isnull=True).order_by("purpose", "code")
    return render(request, "backoffice/ledger.html", {
        "system_accounts": system_accounts,
        "segregation": reports.segregation_summary(),
        "problems": verify_ledger_integrity() if request.GET.get("verify") else None,
        "verified": bool(request.GET.get("verify")),
    })


@staff_member_required
@require_POST
def scheduler_tick(request):
    summary = run_scheduler_tick()
    if summary:
        parts = [f"{action}: {', '.join(draws)}" for action, draws in summary.items()]
        messages.success(request, "Scheduler ran — " + "; ".join(parts))
    else:
        messages.info(request, "Scheduler ran — nothing to do.")
    return redirect("backoffice:dashboard")


@staff_member_required
def spin_queue(request):
    """Closed spin games waiting for the admin to spin, plus recent spins."""
    spin_draws = Draw.objects.filter(game__mode="spin").select_related("game")
    awaiting = list(spin_draws.filter(status=Draw.Status.LOCKED).order_by("closes_at"))
    for d in awaiting:  # what the automatic split will pay, shown before spinning
        d.plan = games.prize_plan(d.game, d.total_stake, games.carryover_account(d.game).balance)
    recent = spin_draws.filter(status=Draw.Status.SETTLED).select_related("spun_by").order_by("-settled_at")[:10]
    open_now = spin_draws.filter(status=Draw.Status.OPEN)
    return render(request, "backoffice/spin.html", {"awaiting": awaiting, "recent": recent, "open_now": open_now})


@staff_member_required
@require_POST
def spin(request, pk):
    if not request.user.has_perm("games.change_draw"):
        return JsonResponse({"ok": False, "error": "Your role is not allowed to spin games."}, status=403)
    try:
        draw = games.spin_draw(pk, staff_user=request.user)
    except games.GameError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    cur = draw.game.currency
    return JsonResponse({
        "ok": True,
        "numbers": draw.winning_numbers,
        "players": draw.ticket_count,
        "sales": format_money(draw.total_stake, cur),
        "prize_pool": format_money(draw.prize_pool, cur),
        "company_share": format_money(draw.house_share, cur),
        "grand_winners": draw.grand_winner_count,
        "consolation_winners": draw.consolation_winner_count,
        "total_paid": format_money(draw.total_prizes, cur),
        "carried_over": format_money(draw.carry_out, cur),
        "company_topup": format_money(draw.house_topup, cur),
        "result_url": f"/results/{draw.pk}/",
    })
