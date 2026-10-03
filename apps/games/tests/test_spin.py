from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import grant_welcome_bonus
from apps.core.tests.helpers import balance, coins, make_user
from apps.games import rng, services
from apps.games.models import Draw, Game, Ticket
from apps.ledger.models import LedgerAccount
from apps.ledger.services import get_system_account, verify_ledger_integrity


def lagos(day, hh, mm=0):
    return timezone.make_aware(datetime(2026, 10, day, hh, mm), timezone.get_current_timezone())


def make_spin_game(**overrides):
    spec = dict(name="NextGen Daily", slug="nextgen-daily", mode=Game.Mode.SPIN, ticket_price=500_00, pick_count=4,
                number_max=90, max_tickets_per_draw=50)
    spec.update(overrides)
    return Game.objects.create(**spec)


def open_spin_batch(game, minutes=60):
    return services.open_next_draw(game, closes_at=timezone.now() + timedelta(minutes=minutes))


def close_batch(draw):
    Draw.objects.filter(pk=draw.pk).update(closes_at=timezone.now() - timedelta(seconds=1))
    return services.lock_draw(draw.pk)


class ScheduleTests(TestCase):
    def setUp(self):
        self.game = make_spin_game()  # 08:00-17:00 and 20:00-24:00

    def test_windows(self):
        self.assertEqual(services.current_window(self.game, lagos(5, 10)), (lagos(5, 8), lagos(5, 17)))
        self.assertIsNone(services.current_window(self.game, lagos(5, 18)))
        self.assertEqual(services.next_window(self.game, lagos(5, 18))[0], lagos(5, 20))
        self.assertEqual(services.current_window(self.game, lagos(5, 23, 30)), (lagos(5, 20), lagos(6, 0)))
        self.assertEqual(services.next_window(self.game, lagos(5, 23, 30))[0], lagos(6, 8))
        # While batch 1 is open the "next game" countdown points at batch 2.
        self.assertEqual(services.next_window(self.game, lagos(5, 10))[0], lagos(5, 20))

    def test_scheduler_opens_batches_on_time_and_waits_for_admin_spin(self):
        services.run_scheduler_tick(now=lagos(5, 7, 59))
        self.assertFalse(self.game.draws.exists())
        services.run_scheduler_tick(now=lagos(5, 8))
        draw = self.game.draws.get()
        self.assertEqual((draw.opens_at, draw.closes_at), (lagos(5, 8), lagos(5, 17)))
        services.run_scheduler_tick(now=lagos(5, 12))
        self.assertEqual(self.game.draws.count(), 1)  # no duplicates

        services.run_scheduler_tick(now=lagos(5, 17))
        draw.refresh_from_db()
        self.assertEqual(draw.status, Draw.Status.LOCKED)  # closed, NOT auto-spun
        services.run_scheduler_tick(now=lagos(5, 20))
        self.assertEqual(self.game.draws.filter(status=Draw.Status.OPEN).get().closes_at, lagos(6, 0))
        draw.refresh_from_db()
        self.assertEqual(draw.status, Draw.Status.LOCKED)

    @override_settings(NEXTGEN={**settings.NEXTGEN, "AUTO_SPIN_AFTER_MINUTES": 30})
    def test_optional_auto_spin(self):
        services.run_scheduler_tick(now=lagos(5, 8))
        draw = self.game.draws.get()
        services.run_scheduler_tick(now=lagos(5, 17))
        services.run_scheduler_tick(now=lagos(5, 17, 20))
        draw.refresh_from_db()
        self.assertEqual(draw.status, Draw.Status.LOCKED)
        services.run_scheduler_tick(now=lagos(5, 17, 31))
        draw.refresh_from_db()
        self.assertEqual(draw.status, Draw.Status.SETTLED)


class PlayTests(TestCase):
    def setUp(self):
        self.game = make_spin_game()
        self.draw = open_spin_batch(self.game)

    def test_play_generates_four_numbers_and_costs_50_coins(self):
        user = make_user(balance=2_000_00)  # 200 coins
        ticket = services.play_spin_game(user, self.game.pk)
        self.assertEqual(len(set(ticket.numbers)), 4)
        self.assertTrue(all(1 <= n <= 90 for n in ticket.numbers))
        self.assertEqual(coins(user), 1_500_00)  # 150 coins left
        self.assertEqual(balance(user), 0)  # winnings untouched
        self.assertTrue(rng.verify_ticket(Ticket.objects.select_related("user").get(pk=ticket.pk)))

    def test_winnings_cannot_pay_for_games(self):
        from apps.core.tests.helpers import fund_winnings

        user = make_user()
        fund_winnings(user, 10_000_00)
        with self.assertRaises(services.NeedsDeposit):
            services.play_spin_game(user, self.game.pk)

    def test_closed_game_cannot_be_played(self):
        close_batch(self.draw)
        user = make_user(balance=2_000_00)
        with self.assertRaisesMessage(services.GameError, "closed"):
            services.play_spin_game(user, self.game.pk)

    def test_welcome_bonus_pays_for_first_game(self):
        user = make_user()
        grant_welcome_bonus(user)
        grant_welcome_bonus(user)  # only ever once
        self.assertEqual(coins(user), 500_00)
        ticket = services.play_spin_game(user, self.game.pk)
        self.assertEqual((coins(user), ticket.bonus_stake), (0, 500_00))
        with self.assertRaises(services.NeedsDeposit):
            services.play_spin_game(user, self.game.pk)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_bonus_is_granted_when_contact_is_verified(self):
        from apps.accounts.services import mark_contact_verified

        user = make_user(tier=User.KycTier.UNVERIFIED)
        mark_contact_verified(user, "email")
        self.assertEqual(user.wallets.get().bonus_balance, 500_00)


class SettlementTests(TestCase):
    """40% of each batch's sales is company profit; the 60% pays winners automatically."""

    def setUp(self):
        self.game = make_spin_game()
        self.draw = open_spin_batch(self.game)
        self.staff = User.objects.create_superuser("ops@example.com", "ops-pass-123")

    def play(self, numbers):
        user = make_user(balance=self.game.ticket_price)
        services.purchase_tickets(user, self.draw.pk, [numbers], quick_pick_flags=[True])
        return user

    def spin(self, winning=(1, 2, 3, 4)):
        close_batch(self.draw)
        with mock.patch.object(rng, "derive_numbers", return_value=list(winning)):
            return services.spin_draw(self.draw.pk, staff_user=self.staff)

    def house(self):
        return get_system_account(LedgerAccount.Purpose.HOUSE_REVENUE, "NGN").balance

    def test_prize_plan(self):
        plan = services.prize_plan(self.game, sales=50_000_00, carry_in=0)
        self.assertEqual(plan, {"sales": 50_000_00, "company_share": 20_000_00, "prize_pool": 30_000_00,
                                "grand_winners": 0, "consolation_winners": 4, "carry_out": 2_000_00})
        plan = services.prize_plan(self.game, sales=200_000_00, carry_in=1_000_00)
        # Pool 121,000 -> grand 100,000 + 3 x 7,000 = 121,000, nothing left.
        self.assertEqual((plan["grand_winners"], plan["consolation_winners"], plan["carry_out"]), (1, 3, 0))

    def test_split_is_exact_and_winners_are_the_closest_tickets(self):
        two = [self.play([1, 2, 50, 60]) for _ in range(2)]
        one = [self.play([1, 70, 71, 72 + i]) for i in range(10)]
        for i in range(88):
            self.play([10 + (i % 30), 41, 42, 43 + (i % 40)])
        draw = self.spin()
        # 100 plays x 500 = 50,000 -> company 20,000 (40%), prizes 30,000 (60%).
        self.assertEqual((draw.house_share, draw.prize_pool), (20_000_00, 30_000_00))
        self.assertEqual(self.house(), 20_000_00)  # exactly 40%, never topped up
        self.assertEqual(draw.house_topup, 0)
        # 30,000 < grand prize, so 4 cash prizes of 7,000; 2,000 carried over.
        self.assertEqual((draw.grand_winner_count, draw.consolation_winner_count, draw.carry_out), (0, 4, 2_000_00))
        for user in two:
            self.assertEqual(balance(user), 7_000_00)  # prize money goes to winnings
        self.assertEqual(sum(1 for u in one if balance(u) == 7_000_00), 2)
        self.assertEqual(services.pool_account(draw).balance, 0)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_grand_prize_when_the_pool_covers_it(self):
        self.game.grand_prize = 10_000_00
        self.game.save()
        best = self.play([1, 2, 3, 70])
        for i in range(39):
            self.play([20, 21, 22, 23 + i % 30])
        draw = self.spin()
        # 40 x 500 = 20,000 -> pool 12,000 -> grand 10,000 to the closest ticket, 2,000 carried.
        self.assertEqual((draw.grand_winner_count, draw.consolation_winner_count), (1, 0))
        self.assertEqual(balance(best), 10_000_00)
        self.assertEqual(best.tickets.get().prize_label, "Grand prize")
        self.assertEqual(self.house(), 8_000_00)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_no_grand_prize_when_not_covered(self):
        self.play([1, 2, 3, 4])  # perfect match, but only 300 of prize money
        draw = self.spin()
        self.assertEqual((draw.grand_winner_count, draw.consolation_winner_count, draw.carry_out), (0, 0, 300_00))

    def test_leftover_feeds_the_next_batch(self):
        for i in range(30):
            self.play([1, 50 + i % 20, 71 + i % 9, 85])
        first = self.spin()  # pool 9,000 -> 1 prize, 2,000 carried
        self.assertEqual(first.carry_out, 2_000_00)
        self.draw = open_spin_batch(self.game)
        for _ in range(20):
            self.play([5, 6, 7, 8])
        second = self.spin(winning=(80, 81, 82, 83))
        # 10,000 sales -> 6,000 + 2,000 carried = 8,000 -> 1 prize, 1,000 carried.
        self.assertEqual((second.carry_in, second.prize_pool, second.consolation_winner_count), (2_000_00, 8_000_00, 1))
        self.assertEqual(verify_ledger_integrity(), [])

    def test_tie_break_is_reproducible_from_published_seeds(self):
        for i in range(5):
            self.play([1, 60 + i, 70 + i, 80 + i])  # five tickets tie on 1 match
        for _ in range(25):
            self.play([20, 21, 22, 23])
        draw = self.spin()  # 15,000 sales -> 9,000 pool -> exactly one prize
        tied = Ticket.objects.filter(draw=draw, match_count=1)
        expected = min(tied, key=lambda t: rng.fair_rank(draw.server_seed, draw.client_seed, t.serial))
        self.assertEqual(Ticket.objects.get(draw=draw, status=Ticket.Status.WON), expected)

    def test_admin_spin_endpoint_and_viewer_role(self):
        import io

        from django.contrib.auth.models import Group
        from django.core.management import call_command

        self.play([1, 2, 3, 9])
        close_batch(self.draw)
        call_command("setup_roles", stdout=io.StringIO())
        viewer = make_user(is_staff=True)
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(reverse("backoffice:spin", args=[self.draw.pk])).status_code, 403)
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("backoffice:spin_queue")), "Prize money")
        data = self.client.post(reverse("backoffice:spin", args=[self.draw.pk])).json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["numbers"]), 4)
        self.assertEqual(self.client.post(reverse("backoffice:spin", args=[self.draw.pk])).status_code, 400)


class AdminGameEditorTests(TestCase):
    def setUp(self):
        self.game = make_spin_game()
        open_spin_batch(self.game)
        self.admin = User.objects.create_superuser("boss@example.com", "Boss-pass-123")
        self.client.force_login(self.admin)
        self.url = f"/admin/games/game/{self.game.pk}/change/"

    def form_data(self, **changes):
        form = self.client.get(self.url).context["adminform"].form
        data = {k: v for k, v in form.initial.items() if v is not None and k != "id"}
        data.update(changes)
        return data

    def test_edit_price_prizes_and_times_in_naira(self):
        response = self.client.post(self.url, self.form_data(
            ticket_price_naira="750", grand_prize_naira="250000", consolation_prize_naira="5000",
            game_times="09:00-16:00, 19:00-23:30", prize_pool_percent="55", is_active="on"))
        self.assertEqual(response.status_code, 302)
        self.game.refresh_from_db()
        self.assertEqual((self.game.ticket_price, self.game.grand_prize, self.game.consolation_prize,
                          self.game.prize_pool_percent), (750_00, 250_000_00, 5_000_00, 55))
        self.assertEqual(self.game.schedule, [["09:00", "16:00"], ["19:00", "23:30"]])
        user = make_user(balance=1_000_00)
        services.play_spin_game(user, self.game.pk)
        self.assertEqual(coins(user), 250_00)

    def test_bad_game_times_rejected(self):
        response = self.client.post(self.url, self.form_data(game_times="17:00-08:00"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("game_times", response.context["adminform"].form.errors)

    def test_admin_home_is_simple(self):
        page = self.client.get("/admin/")
        for text in ("Spin games", "Approve withdrawals", "Review KYC", "Game settings", "Platform settings"):
            self.assertContains(page, text)
        for hidden in ("Journal transactions", "Ledger accounts", "Webhook events"):
            self.assertNotContains(page, hidden)


class PlayerViewTests(TestCase):
    def setUp(self):
        self.game = make_spin_game()
        self.draw = open_spin_batch(self.game)

    def test_play_popup_returns_the_four_numbers(self):
        user = make_user(balance=1_000_00)
        self.client.force_login(user)
        data = self.client.post(reverse("games:play_game", args=[self.game.slug]), {"purchase_token": "p1"},
                                HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertTrue(data["ok"])
        self.assertEqual(user.tickets.get().numbers, data["numbers"])
        self.assertEqual(data["paid"], "50 coins")

    def test_play_popup_low_coins_links_to_buy_coins(self):
        user = make_user(balance=100_00)
        self.client.force_login(user)
        data = self.client.post(reverse("games:play_game", args=[self.game.slug]), {"purchase_token": "p2"},
                                HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertFalse(data["ok"])
        self.assertIn("/wallet/deposit/?coins=", data["deposit_url"])

    def test_players_see_coins_and_naira_prizes_but_never_the_split(self):
        user = make_user(balance=2_000_00)
        self.client.force_login(user)
        home = self.client.get("/").content.decode()
        for text in ("200 coins", "50 coins", "₦100,000.00", "₦7,000.00", "Buy coins", "Your winnings"):
            self.assertIn(text, home)
        detail = self.client.get(reverse("games:game_detail", args=[self.game.slug])).content.decode()
        for page in (home, detail):
            self.assertNotIn("Prize pool", page)
            self.assertNotIn("60%", page)
            self.assertNotIn("40%", page)
        services.play_spin_game(user, self.game.pk)
        close_batch(self.draw)
        services.spin_draw(self.draw.pk)
        result = self.client.get(reverse("games:result_detail", args=[self.draw.pk])).content.decode()
        self.assertNotIn("Prize pool", result)
        self.assertNotIn("carried", result)

    def test_dashboard_shows_details_history_and_kyc(self):
        from apps.accounts.services import submit_kyc

        user = make_user(balance=2_000_00)
        services.play_spin_game(user, self.game.pk)
        submit_kyc(user, id_type="nin", id_number="12345678901", bank_code="058", account_number="0123456789")
        self.client.force_login(user)
        page = self.client.get("/")
        for text in ("My details", user.full_name, "KYC status", "Verified", "Name matches ID", "Games played",
                     "Guaranty Trust Bank"):
            self.assertContains(page, text)

    def test_new_unverified_user_lands_on_dashboard(self):
        user = make_user(tier=User.KycTier.UNVERIFIED)
        user.email_verified = False
        user.save()
        self.client.force_login(user)
        self.assertContains(self.client.get("/"), "Enter code")

    def test_double_click_plays_once(self):
        user = make_user(balance=1_000_00)
        self.client.force_login(user)
        for _ in range(2):
            self.client.post(reverse("games:play_game", args=[self.game.slug]), {"purchase_token": "same"})
        self.assertEqual(user.tickets.count(), 1)
