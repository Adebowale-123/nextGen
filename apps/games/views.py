import json
import uuid

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.core.config import RULES
from apps.core.money import coin_value, format_coins
from apps.core.decorators import verified_required

from . import rng, services
from .models import Draw, Game, Ticket


def coins_needed(shortfall):
    """Whole coins to buy to cover a shortfall (at least the minimum deposit)."""
    need = max(shortfall, RULES["MIN_DEPOSIT"])
    return -(-need // coin_value())


def lobby(request):
    spin_cards, draws = services.lobby()
    return render(request, "games/lobby.html", {"spin_cards": spin_cards, "draws": draws})


def game_detail(request, slug):
    """'View details' for a spin game: rules, live prize pool, countdowns, my plays."""
    game = get_object_or_404(Game, slug=slug, mode=Game.Mode.SPIN)
    card = services.game_overview(game)
    my_tickets = []
    if request.user.is_authenticated and card["draw"]:
        my_tickets = card["draw"].tickets.filter(user=request.user)
    return render(request, "games/game_detail.html", {
        "card": card,
        "game": game,
        "my_tickets": my_tickets,
        "schedule": [(o, c) for o, c in services.schedule_windows(game, timezone.localdate())],
        "recent_results": game.draws.filter(status=Draw.Status.SETTLED).order_by("-closes_at")[:5],
        "purchase_token": uuid.uuid4().hex,
    })


@verified_required
def play_game(request, slug):
    """One click: pay for one play and get 4 numbers generated automatically."""
    game = get_object_or_404(Game, slug=slug, mode=Game.Mode.SPIN)
    if request.method != "POST":
        return redirect("games:game_detail", slug=slug)
    wants_json = request.headers.get("X-Requested-With") == "fetch"  # ticket popup on the page
    try:
        ticket = services.play_spin_game(request.user, game.pk,
                                         idempotency_key=request.POST.get("purchase_token") or None)
    except services.NeedsDeposit as exc:
        deposit_url = f"{reverse('payments:deposit')}?coins={coins_needed(exc.shortfall)}"
        if wants_json:
            return JsonResponse({"ok": False, "error": f"{exc} Add money to play.", "deposit_url": deposit_url})
        messages.warning(request, f"{exc} Add money to continue.")
        return redirect(deposit_url)
    except services.GameError as exc:
        if wants_json:
            return JsonResponse({"ok": False, "error": str(exc)})
        messages.error(request, str(exc))
        return redirect("games:game_detail", slug=slug)
    if wants_json:
        return JsonResponse({
            "ok": True,
            "numbers": ticket.numbers,
            "serial": ticket.serial,
            "game": game.name,
            "paid": format_coins(ticket.stake),
            "from_bonus": ticket.bonus_stake > 0,
            "closes_at": timezone.localtime(ticket.draw.closes_at).strftime("%I:%M %p").lstrip("0"),
            "ticket_url": reverse("games:ticket_detail", args=[ticket.serial]),
        })
    messages.success(request, "You're in! Here are your numbers. Good luck! 🍀")
    return redirect("games:ticket_detail", serial=ticket.serial)


def draw_detail(request, pk):
    draw = get_object_or_404(Draw.objects.select_related("game"), pk=pk)
    if draw.game.is_spin:
        return redirect("games:game_detail", slug=draw.game.slug)
    if draw.status != Draw.Status.OPEN:
        return redirect("games:result_detail", pk=draw.pk)
    game = draw.game

    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect(f"{reverse('accounts:login')}?next={request.path}")
        if not request.user.is_contact_verified:
            return redirect("accounts:verify")
        try:
            lines_data = json.loads(request.POST.get("lines", "[]"))
            lines = [line["numbers"] for line in lines_data]
            quick = [bool(line.get("quick")) for line in lines_data]
        except (ValueError, KeyError, TypeError):
            messages.error(request, "Something went wrong with your selection. Please try again.")
            return redirect("games:draw_detail", pk=draw.pk)
        try:
            tickets = services.purchase_tickets(
                request.user, draw.pk, lines, quick_pick_flags=quick,
                idempotency_key=request.POST.get("purchase_token") or None,
            )
        except services.NeedsDeposit as exc:
            messages.warning(request, f"{exc} Top up to continue.")
            return redirect(f"{reverse('payments:deposit')}?coins={coins_needed(exc.shortfall)}")
        except services.GameError as exc:
            messages.error(request, str(exc))
            return redirect("games:draw_detail", pk=draw.pk)
        messages.success(request, f"You're in! {len(tickets)} ticket(s) purchased for {draw}. Good luck!")
        return redirect("games:my_tickets")

    my_tickets = []
    if request.user.is_authenticated:
        my_tickets = draw.tickets.filter(user=request.user)
    return render(request, "games/draw_detail.html", {
        "draw": draw,
        "game": game,
        "tiers": game.prize_tiers.all(),
        "numbers": range(1, game.number_max + 1),
        "my_tickets": my_tickets,
        "purchase_token": uuid.uuid4().hex,
    })


@verified_required
def my_tickets(request):
    tickets = request.user.tickets.select_related("draw__game", "prize_tier")
    status = request.GET.get("status")
    if status in Ticket.Status.values:
        tickets = tickets.filter(status=status)
    page = Paginator(tickets, 20).get_page(request.GET.get("page"))
    return render(request, "games/my_tickets.html", {"page": page, "status": status,
                                                     "statuses": Ticket.Status.choices})


@verified_required
def ticket_detail(request, serial):
    ticket = get_object_or_404(Ticket.objects.select_related("draw__game", "prize_tier", "user"),
                               serial=serial, user=request.user)
    return render(request, "games/ticket_detail.html", {
        "ticket": ticket,
        "signature_valid": rng.verify_ticket(ticket),
        "winning": ticket.draw.winning_numbers,
    })


def results(request):
    draws = Draw.objects.filter(status__in=[Draw.Status.SETTLED, Draw.Status.DRAWN, Draw.Status.CANCELLED]) \
        .select_related("game").order_by("-closes_at")
    page = Paginator(draws, 20).get_page(request.GET.get("page"))
    return render(request, "games/results.html", {"page": page})


def result_detail(request, pk):
    draw = get_object_or_404(Draw.objects.select_related("game"), pk=pk)
    tier_breakdown = []
    if draw.status == Draw.Status.SETTLED:
        for tier in draw.game.prize_tiers.all():
            won = draw.tickets.filter(prize_tier=tier)
            tier_breakdown.append({"tier": tier, "winners": won.count(),
                                   "paid": sum(won.values_list("prize_amount", flat=True))})
    my_tickets = draw.tickets.filter(user=request.user) if request.user.is_authenticated else []
    return render(request, "games/result_detail.html", {
        "draw": draw,
        "tier_breakdown": tier_breakdown,
        "my_tickets": my_tickets,
    })
