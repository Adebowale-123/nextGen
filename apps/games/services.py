"""Ticket purchase (Flow C) and draw execution / settlement (Flow D)."""

import logging
from collections import defaultdict
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Max
from django.urls import reverse
from django.utils import timezone

from apps.compliance import services as compliance
from apps.core.config import RULES
from apps.core.money import format_coins
from apps.ledger.models import JournalTransaction, LedgerAccount, Wallet
from apps.ledger.services import (
    InsufficientFunds,
    credit,
    debit,
    ensure_bonus_account,
    get_system_account,
    lock_wallet,
    post_transaction,
)
from apps.notifications.services import notify

from . import rng
from .models import Draw, Game, PrizeTier, Ticket

log = logging.getLogger("apps.games")


class GameError(Exception):
    """User-facing game error."""


class NeedsDeposit(GameError):
    """Raised when the wallet can't cover the purchase (the UI offers a deposit)."""

    def __init__(self, message, shortfall):
        super().__init__(message)
        self.shortfall = shortfall


def pool_account(draw):
    return get_system_account(LedgerAccount.Purpose.POOL_HOLD, draw.game.currency, f"draw-{draw.pk}")


# ---------------------------------------------------------------------------
# Ticket purchase
# ---------------------------------------------------------------------------


def validate_line(game, numbers):
    try:
        numbers = sorted(int(n) for n in numbers)
    except (TypeError, ValueError):
        raise GameError("Ticket numbers must be whole numbers.")
    if len(numbers) != game.pick_count:
        raise GameError(f"Pick exactly {game.pick_count} numbers per line.")
    if len(set(numbers)) != len(numbers):
        raise GameError("Numbers on a line must be different.")
    if numbers[0] < 1 or numbers[-1] > game.number_max:
        raise GameError(f"Numbers must be between 1 and {game.number_max}.")
    return numbers


def purchase_tickets(user, draw_id, lines, *, quick_pick_flags=None, idempotency_key=None) -> list[Ticket]:
    """Buy one ticket per line. `lines` is a list of number lists."""
    if not lines:
        raise GameError("Add at least one line.")
    quick_pick_flags = quick_pick_flags or [False] * len(lines)

    with transaction.atomic():
        # Lock order: draw -> wallet -> pool account.
        draw = Draw.objects.select_for_update().select_related("game").filter(pk=draw_id).first()
        if draw is None:
            raise GameError("Draw not found.")
        # Purchases on a draw are serialised by the draw lock, so this
        # double-submit check cannot race.
        if idempotency_key:
            prior = JournalTransaction.objects.filter(idempotency_key=f"purchase:{idempotency_key}").first()
            if prior:
                return list(Ticket.objects.filter(purchase_transaction=prior))
        game = draw.game
        if not draw.is_selling:
            raise GameError("Ticket sales for this draw have closed.")
        if len(lines) > game.max_lines_per_purchase:
            raise GameError(f"You can buy up to {game.max_lines_per_purchase} lines at a time.")
        lines = [validate_line(game, line) for line in lines]
        cost = game.ticket_price * len(lines)
        try:
            compliance.check_can_stake(user, cost)
        except compliance.ComplianceError as exc:
            raise GameError(str(exc)) from exc
        owned = draw.tickets.filter(user=user).count()
        if owned + len(lines) > game.max_tickets_per_draw:
            raise GameError(f"Limit is {game.max_tickets_per_draw} tickets per player per draw.")

        wallet = lock_wallet(user, game.currency)
        bonus = LedgerAccount.objects.select_for_update().get(pk=ensure_bonus_account(wallet).pk)
        # Bonus credit is spent first; the rest comes from cash.
        bonus_used = min(bonus.balance, cost)
        cash_used = cost - bonus_used
        if wallet.account.balance < cash_used:
            shortfall = cash_used - wallet.account.balance
            raise NeedsDeposit(f"You need {format_coins(shortfall)} more to play.", shortfall)

        legs = [credit(pool_account(draw), cost)]
        if bonus_used:
            legs.append(debit(bonus, bonus_used))
        if cash_used:
            legs.append(debit(wallet.account, cash_used))
        try:
            txn = post_transaction(
                JournalTransaction.Type.TICKET_PURCHASE,
                legs,
                description=f"{len(lines)} ticket(s) · {draw}",
                idempotency_key=f"purchase:{idempotency_key}" if idempotency_key else None,
                source=draw,
            )
        except InsufficientFunds as exc:
            raise NeedsDeposit(str(exc), cost) from exc

        now = timezone.now()
        tickets = []
        bonus_left = bonus_used
        for numbers, is_qp in zip(lines, quick_pick_flags):
            ticket_bonus = min(bonus_left, game.ticket_price)
            bonus_left -= ticket_bonus
            ticket = Ticket(
                user=user, draw=draw, numbers=numbers, is_quick_pick=bool(is_qp), stake=game.ticket_price,
                bonus_stake=ticket_bonus, purchased_at=now, purchase_transaction=txn,
            )
            ticket.signature = rng.sign_ticket(
                serial=ticket.serial, user_public_id=user.public_id, draw_id=draw.pk, purchased_at=now,
                numbers=numbers,
            )
            tickets.append(ticket)
        Ticket.objects.bulk_create(tickets)

        wallet.total_staked += cost
        # Only cash play counts towards the AML play-through requirement.
        wallet.wagering_remaining = max(0, wallet.wagering_remaining - cash_used)
        wallet.save(update_fields=["total_staked", "wagering_remaining"])
        draw.ticket_count += len(tickets)
        draw.total_stake += cost
        draw.save(update_fields=["ticket_count", "total_stake"])
    return tickets


# ---------------------------------------------------------------------------
# Draw lifecycle
# ---------------------------------------------------------------------------


def next_close_time(game, after):
    local = timezone.localtime(after)
    candidate = timezone.make_aware(datetime.combine(local.date(), game.draw_time), local.tzinfo)
    if candidate <= after + timedelta(minutes=5):
        candidate = timezone.make_aware(
            datetime.combine(local.date() + timedelta(days=1), game.draw_time), local.tzinfo
        )
    return candidate


def open_next_draw(game, *, closes_at=None, opens_at=None, now=None) -> Draw:
    now = now or timezone.now()
    with transaction.atomic():
        Game.objects.select_for_update().get(pk=game.pk)  # serialise draw-number allocation
        existing = game.draws.filter(status=Draw.Status.OPEN, closes_at__gt=now).first()
        if existing and closes_at is None:
            return existing
        last = game.draws.aggregate(n=Max("draw_number"))["n"] or 0
        draw = Draw.objects.create(
            game=game, draw_number=last + 1, opens_at=opens_at or now,
            closes_at=closes_at or next_close_time(game, now),
        )
    log.info("Opened %s closing %s (seed hash %s)", draw, draw.closes_at, draw.server_seed_hash)
    return draw


def lock_draw(draw_id, now=None) -> Draw:
    """Close sales and fix the client seed from all sold tickets."""
    now = now or timezone.now()
    with transaction.atomic():
        draw = Draw.objects.select_for_update().get(pk=draw_id)
        if draw.status != Draw.Status.OPEN or draw.closes_at > now:
            return draw
        signatures = list(draw.tickets.values_list("signature", flat=True))
        draw.client_seed = rng.compute_client_seed(signatures, draw.nonce)
        draw.status = Draw.Status.LOCKED
        draw.locked_at = now
        draw.save(update_fields=["client_seed", "status", "locked_at"])
    return draw


def execute_draw(draw_id) -> Draw:
    with transaction.atomic():
        draw = Draw.objects.select_for_update().select_related("game").get(pk=draw_id)
        if draw.status != Draw.Status.LOCKED:
            return draw
        draw.winning_numbers = rng.derive_numbers(
            draw.server_seed, draw.client_seed, draw.nonce, draw.game.pick_count, draw.game.number_max
        )
        draw.status = Draw.Status.DRAWN
        draw.drawn_at = timezone.now()
        draw.save(update_fields=["winning_numbers", "status", "drawn_at"])
    log.info("Drew %s: %s", draw, draw.winning_numbers)
    return draw


def calculate_prizes(draw, tickets, tiers):
    """Return {ticket: (tier, prize_amount)} for winners, setting match_count on every ticket."""
    winning = set(draw.winning_numbers)
    by_tier = defaultdict(list)
    for ticket in tickets:
        ticket.match_count = len(winning.intersection(ticket.numbers))
        tier = tiers.get(ticket.match_count)
        if tier:
            by_tier[tier].append(ticket)

    prizes = {}
    for tier, winners in by_tier.items():
        if tier.payout_type == PrizeTier.PayoutType.FIXED_MULTIPLIER:
            for ticket in winners:
                prizes[ticket] = (tier, int(ticket.stake * tier.value))
        else:
            tier_pool = int(draw.total_stake * tier.value / 100)
            share = tier_pool // len(winners)
            for ticket in winners:
                prizes[ticket] = (tier, share)
    return {t: p for t, p in prizes.items() if p[1] > 0}


def settle_draw(draw_id) -> Draw:
    mode = Draw.objects.filter(pk=draw_id).values_list("game__mode", flat=True).first()
    if mode == Game.Mode.SPIN:
        return settle_spin_draw(draw_id)
    return _settle_pick_draw(draw_id)


def _settle_pick_draw(draw_id) -> Draw:
    winners_to_notify = []
    with transaction.atomic():
        draw = Draw.objects.select_for_update().select_related("game").get(pk=draw_id)
        if draw.status != Draw.Status.DRAWN:
            return draw
        game = draw.game
        tiers = {t.match_count: t for t in game.prize_tiers.all()}
        tickets = list(draw.tickets.filter(status=Ticket.Status.ACTIVE).select_related("user"))
        prizes = calculate_prizes(draw, tickets, tiers)
        total_prizes = sum(amount for _, amount in prizes.values())

        pool = pool_account(draw)
        house = get_system_account(LedgerAccount.Purpose.HOUSE_REVENUE, game.currency)
        pool.refresh_from_db()
        if total_prizes > pool.balance:
            # Fixed-odds prizes exceeded stakes: the house funds the shortfall.
            post_transaction(
                JournalTransaction.Type.POOL_TOPUP,
                [debit(house, total_prizes - pool.balance), credit(pool, total_prizes - pool.balance)],
                description=f"House top-up for {draw}",
                idempotency_key=f"pool-topup:{draw.pk}",
                source=draw,
            )

        wallets = {
            w.user_id: w
            for w in Wallet.objects.filter(user_id__in={t.user_id for t in prizes}, currency=game.currency)
            .select_related("account")
        }
        for ticket in tickets:
            win = prizes.get(ticket)
            if not win:
                ticket.status = Ticket.Status.LOST
                continue
            tier, amount = win
            wallet = wallets[ticket.user_id]
            ticket.payout_transaction = post_transaction(
                JournalTransaction.Type.PRIZE_PAYOUT,
                [debit(pool, amount), credit(wallet.account, amount)],
                description=f"{tier.name} win · {draw} · ticket {ticket.serial}",
                idempotency_key=f"prize:{ticket.serial}",
                source=ticket,
            )
            Wallet.objects.filter(pk=wallet.pk).update(total_won=F("total_won") + amount)
            ticket.status = Ticket.Status.WON
            ticket.prize_tier = tier
            ticket.prize_amount = amount
            winners_to_notify.append(ticket)

        pool.refresh_from_db()
        if pool.balance > 0:
            post_transaction(
                JournalTransaction.Type.POOL_SETTLEMENT,
                [debit(pool, pool.balance), credit(house, pool.balance)],
                description=f"Pool settlement {draw}",
                idempotency_key=f"pool-settle:{draw.pk}",
                source=draw,
            )
        Ticket.objects.bulk_update(
            tickets, ["status", "match_count", "prize_tier", "prize_amount", "payout_transaction"]
        )
        draw.total_prizes = total_prizes
        draw.winner_count = len(winners_to_notify)
        draw.status = Draw.Status.SETTLED
        draw.settled_at = timezone.now()
        draw.save(update_fields=["total_prizes", "winner_count", "status", "settled_at"])

        for ticket in winners_to_notify:
            notify(
                ticket.user,
                f"You won {format_coins(ticket.prize_amount)}! 🎉",
                f"Ticket {ticket.serial} matched {ticket.match_count} in {draw} ({ticket.prize_tier.name}). "
                "Winnings are in your wallet.",
                kind="win",
                link=reverse("games:ticket_detail", args=[ticket.serial]),
                sms=True,
            )
    log.info("Settled %s: %s winners, prizes %s", draw, len(winners_to_notify), total_prizes)
    return draw


def cancel_draw(draw_id, reason, staff_user=None) -> Draw:
    """Void an unsettled draw and refund every ticket (bonus stakes go back to bonus credit)."""
    with transaction.atomic():
        draw = Draw.objects.select_for_update().select_related("game").get(pk=draw_id)
        if draw.status not in (Draw.Status.OPEN, Draw.Status.LOCKED):
            raise GameError("Only open or locked draws can be cancelled.")
        pool = pool_account(draw)
        refunds = defaultdict(lambda: [0, 0])  # user_id -> [cash, bonus]
        for ticket in draw.tickets.filter(status=Ticket.Status.ACTIVE):
            refunds[ticket.user_id][0] += ticket.stake - ticket.bonus_stake
            refunds[ticket.user_id][1] += ticket.bonus_stake
        wallets = Wallet.objects.filter(user_id__in=refunds, currency=draw.game.currency).select_related("account", "user")
        for wallet in wallets:
            cash, bonus = refunds[wallet.user_id]
            legs = [debit(pool, cash + bonus)]
            if cash:
                legs.append(credit(wallet.account, cash))
            if bonus:
                legs.append(credit(ensure_bonus_account(wallet), bonus))
            post_transaction(
                JournalTransaction.Type.TICKET_REFUND, legs,
                description=f"Refund · {draw} cancelled",
                idempotency_key=f"refund:{draw.pk}:{wallet.user_id}",
                source=draw,
                created_by=staff_user,
            )
            Wallet.objects.filter(pk=wallet.pk).update(
                total_staked=F("total_staked") - (cash + bonus), wagering_remaining=F("wagering_remaining") + cash
            )
            notify(wallet.user, "Game cancelled — refunded",
                   f"{draw} was cancelled. {format_coins(cash + bonus)} has been refunded.",
                   kind="wallet")
        draw.tickets.filter(status=Ticket.Status.ACTIVE).update(status=Ticket.Status.REFUNDED)
        draw.status = Draw.Status.CANCELLED
        draw.cancel_reason = reason[:255]
        draw.save(update_fields=["status", "cancel_reason"])
    return draw


# ---------------------------------------------------------------------------
# Spin games: daily play windows, automatic numbers, admin spin, 60/40 pool
# ---------------------------------------------------------------------------


def schedule_windows(game, day):
    """Play windows on a local calendar day as aware (opens_at, closes_at) pairs."""
    tz = timezone.get_current_timezone()
    base = datetime.combine(day, time.min)
    windows = []
    for start, end in game.schedule or []:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        windows.append((
            timezone.make_aware(base + timedelta(hours=sh, minutes=sm), tz),
            timezone.make_aware(base + timedelta(hours=eh, minutes=em), tz),
        ))
    return sorted(windows)


def current_window(game, now):
    today = timezone.localtime(now).date()
    for day in (today - timedelta(days=1), today):
        for opens_at, closes_at in schedule_windows(game, day):
            if opens_at <= now < closes_at:
                return opens_at, closes_at
    return None


def next_window(game, now):
    today = timezone.localtime(now).date()
    for offset in range(3):
        for opens_at, closes_at in schedule_windows(game, today + timedelta(days=offset)):
            if opens_at > now:
                return opens_at, closes_at
    return None


def clock(dt):
    """8:00 PM style local time."""
    return timezone.localtime(dt).strftime("%I:%M %p").lstrip("0")


def carryover_account(game):
    return get_system_account(LedgerAccount.Purpose.PRIZE_CARRYOVER, game.currency, f"game-{game.pk}")


def open_batch(game, now=None):
    """The draw that is selling right now, if any."""
    now = now or timezone.now()
    return (
        game.draws.filter(status=Draw.Status.OPEN, opens_at__lte=now, closes_at__gt=now).order_by("closes_at").first()
    )


def play_spin_game(user, game_id, *, idempotency_key=None) -> Ticket:
    """One play = one ticket with numbers generated automatically for the player."""
    game = Game.objects.filter(pk=game_id, is_active=True, mode=Game.Mode.SPIN).first()
    if game is None:
        raise GameError("Game not found.")
    draw = open_batch(game)
    if draw is None:
        upcoming = next_window(game, timezone.now())
        hint = f" The next game opens at {clock(upcoming[0])}." if upcoming else ""
        raise GameError(f"{game.name} is closed right now.{hint}")
    numbers = rng.quick_pick(game.pick_count, game.number_max)
    return purchase_tickets(user, draw.pk, [numbers], quick_pick_flags=[True], idempotency_key=idempotency_key)[0]


def spin_draw(draw_id, staff_user=None, consolation_winners=None) -> Draw:
    """Admin action: draw the winning numbers for a closed batch and pay the winners.

    `consolation_winners` overrides the game's setting for this spin only (None = use the game setting).
    """
    if consolation_winners is not None and consolation_winners < 0:
        raise GameError("Number of winners cannot be negative.")
    with transaction.atomic():
        draw = Draw.objects.select_for_update().get(pk=draw_id)
        if draw.status != Draw.Status.LOCKED:
            raise GameError("Only closed games awaiting a spin can be spun.")
        draw.spun_by = staff_user
        draw.consolation_target = consolation_winners
        draw.save(update_fields=["spun_by", "consolation_target"])
    execute_draw(draw_id)
    return settle_draw(draw_id)


def settle_spin_draw(draw_id) -> Draw:
    """Split sales 60/40, pay the grand prize, then consolation prizes until the pool runs out."""
    winners = []
    with transaction.atomic():
        draw = Draw.objects.select_for_update().select_related("game").get(pk=draw_id)
        if draw.status != Draw.Status.DRAWN:
            return draw
        game = draw.game
        tickets = list(draw.tickets.filter(status=Ticket.Status.ACTIVE).select_related("user"))
        pool = pool_account(draw)
        house = get_system_account(LedgerAccount.Purpose.HOUSE_REVENUE, game.currency)
        carry = carryover_account(game)

        sales = draw.total_stake
        pool_share = sales * game.prize_pool_percent // 100
        house_share = sales - pool_share
        carry.refresh_from_db()
        carry_in = carry.balance
        if carry_in:
            post_transaction(JournalTransaction.Type.PRIZE_CARRYOVER, [debit(carry, carry_in), credit(pool, carry_in)],
                             description=f"Prize money carried into {draw}", idempotency_key=f"carry-in:{draw.pk}",
                             source=draw)
        if house_share:
            post_transaction(JournalTransaction.Type.POOL_SETTLEMENT,
                             [debit(pool, house_share), credit(house, house_share)],
                             description=f"Company share ({100 - game.prize_pool_percent}%) · {draw}",
                             idempotency_key=f"pool-settle:{draw.pk}", source=draw)
        prize_pool = pool_share + carry_in

        winning = set(draw.winning_numbers)
        for ticket in tickets:
            ticket.match_count = len(winning.intersection(ticket.numbers))

        grand = [t for t in tickets if t.match_count >= game.pick_count]
        grand_total = len(grand) * game.grand_prize

        # Consolation: best matches first; ties broken by a verifiable seed-based random order.
        qualifiers = [t for t in tickets if game.consolation_min_match <= t.match_count < game.pick_count]
        qualifiers.sort(key=lambda t: (-t.match_count, rng.fair_rank(draw.server_seed, draw.client_seed, t.serial)))
        target = draw.consolation_target if draw.consolation_target is not None else game.consolation_winners
        if target is None:  # automatic: as many as the pool can pay after the grand prize
            remaining = max(0, prize_pool - grand_total)
            target = remaining // game.consolation_prize if game.consolation_prize else 0
        consolation = qualifiers[:target]

        needed = grand_total + len(consolation) * game.consolation_prize
        topup = max(0, needed - prize_pool)
        carry_out = max(0, prize_pool - needed)
        if topup:
            # Guaranteed grand prize / admin-chosen winner count: the company covers any shortfall.
            post_transaction(JournalTransaction.Type.POOL_TOPUP, [debit(house, topup), credit(pool, topup)],
                             description=f"Company top-up of prize pool · {draw}",
                             idempotency_key=f"pool-topup:{draw.pk}", source=draw)

        awards = [(t, game.grand_prize, "Grand prize") for t in grand]
        awards += [(t, game.consolation_prize, "Consolation prize") for t in consolation]
        wallets = {
            w.user_id: w
            for w in Wallet.objects.filter(user_id__in={t.user_id for t, _, _ in awards}, currency=game.currency)
            .select_related("account")
        }
        for ticket in tickets:
            ticket.status = Ticket.Status.LOST
        for ticket, amount, label in awards:
            wallet = wallets[ticket.user_id]
            ticket.payout_transaction = post_transaction(
                JournalTransaction.Type.PRIZE_PAYOUT, [debit(pool, amount), credit(wallet.account, amount)],
                description=f"{label} · {draw} · ticket {ticket.serial}",
                idempotency_key=f"prize:{ticket.serial}", source=ticket,
            )
            Wallet.objects.filter(pk=wallet.pk).update(total_won=F("total_won") + amount)
            ticket.status = Ticket.Status.WON
            ticket.prize_amount = amount
            ticket.prize_label = label
            winners.append(ticket)
        if carry_out:
            post_transaction(JournalTransaction.Type.PRIZE_CARRYOVER, [debit(pool, carry_out), credit(carry, carry_out)],
                             description=f"Unused prize money carried to next batch · {draw}",
                             idempotency_key=f"carry-out:{draw.pk}", source=draw)

        Ticket.objects.bulk_update(tickets, ["status", "match_count", "prize_amount", "prize_label",
                                             "payout_transaction"])
        draw.prize_pool = prize_pool
        draw.house_share = house_share
        draw.carry_in = carry_in
        draw.carry_out = carry_out
        draw.house_topup = topup
        draw.grand_winner_count = len(grand)
        draw.consolation_winner_count = len(consolation)
        draw.total_prizes = sum(amount for _, amount, _ in awards)
        draw.winner_count = len(awards)
        draw.status = Draw.Status.SETTLED
        draw.settled_at = timezone.now()
        draw.save()

        for ticket in winners:
            notify(
                ticket.user,
                f"You won {format_coins(ticket.prize_amount)}! 🎉",
                f"{ticket.prize_label} in {game.name}. The money is in your wallet.",
                kind="win", link=reverse("games:ticket_detail", args=[ticket.serial]), sms=True,
            )
    log.info("Settled %s: pool %s, %s grand, %s consolation, carry %s",
             draw, draw.prize_pool, draw.grand_winner_count, draw.consolation_winner_count, draw.carry_out)
    return draw


def game_overview(game, now=None):
    """Everything a game card needs: open batch, countdowns, live prize pool."""
    now = now or timezone.now()
    draw = open_batch(game, now)
    upcoming = next_window(game, now)
    carry = carryover_account(game).balance
    sales = draw.total_stake if draw else 0
    return {
        "game": game,
        "draw": draw,
        "is_open": draw is not None,
        "closes_at": draw.closes_at if draw else None,
        "next_opens_at": upcoming[0] if upcoming else None,
        "awaiting_spin": game.draws.filter(status=Draw.Status.LOCKED).exists(),
        "prize_pool": sales * game.prize_pool_percent // 100 + carry,
        "last_result": game.draws.filter(status=Draw.Status.SETTLED).order_by("-closes_at").first(),
    }


def lobby(now=None):
    """(spin game overviews, open pick-game draws) for the home and play pages."""
    now = now or timezone.now()
    games = Game.objects.filter(is_active=True)
    spin_cards = [game_overview(g, now) for g in games if g.is_spin]
    pick_draws = (
        Draw.objects.filter(status=Draw.Status.OPEN, closes_at__gt=now, game__is_active=True,
                            game__mode=Game.Mode.PICK)
        .select_related("game").prefetch_related("game__prize_tiers")
    )
    return spin_cards, pick_draws


def run_scheduler_tick(now=None) -> dict:
    """Advance every draw through its lifecycle. Safe to run concurrently/repeatedly."""
    now = now or timezone.now()
    summary = defaultdict(list)
    for draw_id in Draw.objects.filter(status=Draw.Status.OPEN, closes_at__lte=now).values_list("pk", flat=True):
        summary["locked"].append(str(lock_draw(draw_id, now)))

    auto_spin = RULES["AUTO_SPIN_AFTER_MINUTES"]
    for draw in Draw.objects.filter(status=Draw.Status.LOCKED).select_related("game"):
        if not draw.game.is_spin:
            summary["drawn"].append(str(execute_draw(draw.pk)))
        elif auto_spin and draw.locked_at and draw.locked_at <= now - timedelta(minutes=auto_spin):
            summary["auto-spun"].append(str(spin_draw(draw.pk)))
    for draw_id in Draw.objects.filter(status=Draw.Status.DRAWN).values_list("pk", flat=True):
        summary["settled"].append(str(settle_draw(draw_id)))

    for game in Game.objects.filter(is_active=True):
        if game.is_spin:
            window = current_window(game, now)
            if window and not game.draws.filter(opens_at=window[0]).exists():
                summary["opened"].append(str(open_next_draw(game, opens_at=window[0], closes_at=window[1], now=now)))
        elif not game.draws.filter(status=Draw.Status.OPEN, closes_at__gt=now).exists():
            summary["opened"].append(str(open_next_draw(game, now=now)))
    return dict(summary)
