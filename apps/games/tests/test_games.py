from collections import Counter
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.tests.helpers import balance, coins, make_game, make_open_draw, make_user
from apps.games import rng, services
from apps.games.models import Draw, Ticket
from apps.ledger.models import LedgerAccount
from apps.ledger.services import get_system_account, verify_ledger_integrity


class RngTests(TestCase):
    def test_deterministic_unique_and_in_range(self):
        a = rng.derive_numbers("seed", "client", "n-1", 6, 49)
        self.assertEqual(a, rng.derive_numbers("seed", "client", "n-1", 6, 49))
        self.assertEqual(len(set(a)), 6)
        self.assertTrue(all(1 <= n <= 49 for n in a))
        self.assertNotEqual(a, rng.derive_numbers("seed", "client", "n-2", 6, 49))

    def test_can_draw_entire_range(self):
        self.assertEqual(sorted(rng.derive_numbers("s", "c", "n", 20, 20)), list(range(1, 21)))

    def test_roughly_uniform(self):
        counts = Counter()
        for i in range(3000):
            counts.update(rng.derive_numbers("seed", str(i), "n", 1, 10))
        self.assertEqual(set(counts), set(range(1, 11)))
        self.assertTrue(all(220 < c < 380 for c in counts.values()), counts)

    def test_quick_pick(self):
        picks = rng.quick_pick(5, 30)
        self.assertEqual(len(set(picks)), 5)
        self.assertEqual(picks, sorted(picks))


class PurchaseTests(TestCase):
    def setUp(self):
        self.user = make_user(balance=10_000_00)
        self.draw = make_open_draw()

    def test_purchase_debits_wallet_and_signs_tickets(self):
        tickets = services.purchase_tickets(self.user, self.draw.pk, [[1, 2, 3, 4, 5], [30, 29, 28, 27, 26]])
        self.assertEqual(len(tickets), 2)
        self.assertEqual(coins(self.user), 10_000_00 - 200_00)
        self.assertEqual(tickets[1].numbers, [26, 27, 28, 29, 30])
        for t in Ticket.objects.select_related("user"):
            self.assertTrue(rng.verify_ticket(t))
        pool = services.pool_account(self.draw)
        self.assertEqual(pool.balance, 200_00)
        self.draw.refresh_from_db()
        self.assertEqual((self.draw.ticket_count, self.draw.total_stake), (2, 200_00))

    def test_tampered_ticket_fails_signature(self):
        services.purchase_tickets(self.user, self.draw.pk, [[1, 2, 3, 4, 5]])
        ticket = Ticket.objects.select_related("user").get()
        ticket.numbers = [1, 2, 3, 4, 6]
        self.assertFalse(rng.verify_ticket(ticket))

    def test_insufficient_balance_prompts_deposit(self):
        poor = make_user(balance=50_00)
        with self.assertRaises(services.NeedsDeposit) as ctx:
            services.purchase_tickets(poor, self.draw.pk, [[1, 2, 3, 4, 5]])
        self.assertEqual(ctx.exception.shortfall, 50_00)
        self.assertEqual(coins(poor), 50_00)

    def test_invalid_lines_rejected(self):
        for bad in ([1, 2, 3, 4], [1, 1, 2, 3, 4], [0, 1, 2, 3, 4], [1, 2, 3, 4, 31]):
            with self.assertRaises(services.GameError):
                services.purchase_tickets(self.user, self.draw.pk, [bad])
        self.assertEqual(coins(self.user), 10_000_00)

    def test_closed_draw_rejected(self):
        Draw.objects.filter(pk=self.draw.pk).update(closes_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaisesMessage(services.GameError, "closed"):
            services.purchase_tickets(self.user, self.draw.pk, [[1, 2, 3, 4, 5]])

    def test_tier1_stake_cap(self):
        rich = make_user(balance=100_000_00)
        draw = make_open_draw(make_game(ticket_price=1_500_00))
        with self.assertRaisesMessage(services.GameError, "Tier 1"):
            services.purchase_tickets(rich, draw.pk, [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]])

    def test_idempotent_purchase(self):
        services.purchase_tickets(self.user, self.draw.pk, [[1, 2, 3, 4, 5]], idempotency_key="abc")
        services.purchase_tickets(self.user, self.draw.pk, [[1, 2, 3, 4, 5]], idempotency_key="abc")
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(coins(self.user), 10_000_00 - 100_00)

    def test_unverified_user_cannot_play(self):
        user = make_user(tier=User.KycTier.UNVERIFIED, balance=1_000_00)
        with self.assertRaisesMessage(services.GameError, "Verify"):
            services.purchase_tickets(user, self.draw.pk, [[1, 2, 3, 4, 5]])


class DrawLifecycleTests(TestCase):
    def setUp(self):
        self.game = make_game()  # jackpot 50% pool, match4 200x, match3 10x
        self.draw = make_open_draw(self.game)
        self.alice = make_user(balance=5_000_00)
        self.bob = make_user(balance=5_000_00)
        self.carol = make_user(balance=5_000_00)
        services.purchase_tickets(self.alice, self.draw.pk, [[1, 2, 3, 4, 5]])      # jackpot
        services.purchase_tickets(self.bob, self.draw.pk, [[1, 2, 3, 4, 5]])        # jackpot (split)
        services.purchase_tickets(self.carol, self.draw.pk, [[1, 2, 3, 20, 21],     # match 3
                                                             [10, 11, 12, 13, 14]])  # nothing
        Draw.objects.filter(pk=self.draw.pk).update(closes_at=timezone.now() - timedelta(seconds=1))

    def run_draw(self, winning=(1, 2, 3, 4, 5)):
        with mock.patch.object(rng, "derive_numbers", return_value=list(winning)):
            return services.run_scheduler_tick()

    def test_full_lifecycle_settles_and_pays(self):
        summary = self.run_draw()
        self.assertIn(str(self.draw), summary["settled"])
        self.draw.refresh_from_db()
        self.assertEqual(self.draw.status, Draw.Status.SETTLED)
        self.assertTrue(self.draw.client_seed)

        # Pool = 4 tickets x 100 = 400. Jackpot 50% = 200 split by 2 = 100 each. Match 3 = 10 x 100 = 1000.
        # Tickets were paid with coins; prizes land in withdrawable winnings.
        self.assertEqual((coins(self.alice), balance(self.alice)), (5_000_00 - 100_00, 100_00))
        self.assertEqual(balance(self.bob), 100_00)
        self.assertEqual((coins(self.carol), balance(self.carol)), (5_000_00 - 200_00, 1_000_00))
        self.assertEqual(self.draw.total_prizes, 1_200_00)
        self.assertEqual(self.draw.winner_count, 3)

        statuses = Counter(Ticket.objects.values_list("status", flat=True))
        self.assertEqual(statuses, {Ticket.Status.WON: 3, Ticket.Status.LOST: 1})

        # Prizes (1200) exceeded stakes (400): the house topped up 800, pool ends at zero.
        house = get_system_account(LedgerAccount.Purpose.HOUSE_REVENUE, "NGN")
        self.assertEqual(house.balance, -800_00)
        self.assertEqual(services.pool_account(self.draw).balance, 0)
        self.assertEqual(self.alice.wallets.get().total_won, 100_00)
        self.assertEqual(verify_ledger_integrity(), [])

        # A new draw opened for the game.
        self.assertTrue(self.game.draws.filter(status=Draw.Status.OPEN).exists())

    def test_no_winners_sends_pool_to_house(self):
        self.run_draw(winning=(25, 26, 27, 28, 29))
        house = get_system_account(LedgerAccount.Purpose.HOUSE_REVENUE, "NGN")
        self.assertEqual(house.balance, 400_00)
        self.assertEqual(Ticket.objects.filter(status=Ticket.Status.LOST).count(), 4)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_settlement_is_idempotent(self):
        self.run_draw()
        before = balance(self.carol)
        services.settle_draw(self.draw.pk)
        self.run_draw()
        self.assertEqual(balance(self.carol), before)

    def test_real_rng_matches_published_seed(self):
        services.run_scheduler_tick()
        self.draw.refresh_from_db()
        self.assertEqual(rng.sha256_hex(self.draw.server_seed), self.draw.server_seed_hash)
        self.assertEqual(
            self.draw.winning_numbers,
            rng.derive_numbers(self.draw.server_seed, self.draw.client_seed, self.draw.nonce, 5, 30),
        )

    def test_cancel_refunds_everyone(self):
        services.cancel_draw(self.draw.pk, "Technical issue")
        self.assertEqual(coins(self.alice), 5_000_00)  # refunded as coins
        self.assertEqual(coins(self.carol), 5_000_00)
        self.assertFalse(Ticket.objects.exclude(status=Ticket.Status.REFUNDED).exists())
        self.assertEqual(services.pool_account(self.draw).balance, 0)
        self.assertEqual(verify_ledger_integrity(), [])


class GameViewTests(TestCase):
    def test_buy_via_view_and_redirect_to_deposit_when_short(self):
        draw = make_open_draw()
        user = make_user(balance=100_00)
        self.client.force_login(user)
        url = reverse("games:draw_detail", args=[draw.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        lines = '[{"numbers":[1,2,3,4,5],"quick":false}]'
        response = self.client.post(url, {"lines": lines, "purchase_token": "t1"})
        self.assertRedirects(response, reverse("games:my_tickets"))
        response = self.client.post(url, {"lines": lines, "purchase_token": "t2"})
        self.assertTrue(response["Location"].startswith(reverse("payments:deposit")))
