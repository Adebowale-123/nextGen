from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import grant_welcome_bonus
from apps.core.tests.helpers import balance, make_user
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

    def test_play_generates_four_numbers_and_charges_500(self):
        user = make_user(balance=2_000_00)
        ticket = services.play_spin_game(user, self.game.pk)
        self.assertEqual(len(set(ticket.numbers)), 4)
        self.assertTrue(all(1 <= n <= 90 for n in ticket.numbers))
        self.assertEqual(balance(user), 1_500_00)
        self.assertTrue(rng.verify_ticket(Ticket.objects.select_related("user").get(pk=ticket.pk)))

    def test_closed_game_cannot_be_played(self):
        close_batch(self.draw)
        user = make_user(balance=2_000_00)
        with self.assertRaisesMessage(services.GameError, "closed"):
            services.play_spin_game(user, self.game.pk)

    def test_insufficient_balance_prompts_deposit(self):
        user = make_user(balance=100_00)
        with self.assertRaises(services.NeedsDeposit):
            services.play_spin_game(user, self.game.pk)

    def test_welcome_bonus_pays_for_first_game_and_is_not_cash(self):
        user = make_user()
        grant_welcome_bonus(user)
        grant_welcome_bonus(user)  # only ever once
        wallet = user.wallets.select_related("account", "bonus_account").get()
        self.assertEqual((wallet.balance, wallet.bonus_balance), (0, 500_00))

        ticket = services.play_spin_game(user, self.game.pk)
        wallet = user.wallets.select_related("account", "bonus_account").get()
        self.assertEqual((wallet.balance, wallet.bonus_balance), (0, 0))
        self.assertEqual(ticket.bonus_stake, 500_00)
        with self.assertRaises(services.NeedsDeposit):
            services.play_spin_game(user, self.game.pk)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_bonus_is_granted_when_contact_is_verified(self):
        from apps.accounts.services import mark_contact_verified

        user = make_user(tier=User.KycTier.UNVERIFIED)
        mark_contact_verified(user, "email")
        self.assertEqual(user.wallets.get().bonus_balance, 500_00)


class SpinSettlementTests(TestCase):
    """Sales are split 60/40; grand prize first, then ₦7,000 consolations until the pool runs out."""

    def setUp(self):
        self.game = make_spin_game()
        self.draw = open_spin_batch(self.game)
        self.staff = User.objects.create_superuser("ops@example.com", "ops-pass-123")

    def play(self, numbers, user=None):
        user = user or make_user(balance=500_00)
        services.purchase_tickets(user, self.draw.pk, [numbers], quick_pick_flags=[True])
        return user

    def spin(self, winning=(1, 2, 3, 4)):
        close_batch(self.draw)
        with mock.patch.object(rng, "derive_numbers", return_value=list(winning)):
            return services.spin_draw(self.draw.pk, staff_user=self.staff)

    def test_pool_split_consolation_cap_and_carryover(self):
        two_matchers = [self.play([1, 2, 50, 60]) for _ in range(2)]
        one_matchers = [self.play([1, 70, 71, 72 + i]) for i in range(10)]
        for i in range(88):
            self.play([10 + (i % 30), 41, 42, 43 + (i % 40)])  # no matches
        draw = self.spin()

        # 100 plays x ₦500 = ₦50,000 -> pool ₦30,000 (60%), company ₦20,000 (40%).
        self.assertEqual(draw.total_stake, 50_000_00)
        self.assertEqual(draw.prize_pool, 30_000_00)
        self.assertEqual(draw.house_share, 20_000_00)
        # ₦30,000 // ₦7,000 = 4 consolation prizes; best matches first.
        self.assertEqual((draw.grand_winner_count, draw.consolation_winner_count), (0, 4))
        self.assertEqual(draw.total_prizes, 28_000_00)
        self.assertEqual(draw.carry_out, 2_000_00)
        for user in two_matchers:
            self.assertEqual(balance(user), 7_000_00)
        self.assertEqual(sum(1 for u in one_matchers if balance(u) == 7_000_00), 2)

        house = get_system_account(LedgerAccount.Purpose.HOUSE_REVENUE, "NGN")
        self.assertEqual(house.balance, 20_000_00)
        self.assertEqual(services.pool_account(draw).balance, 0)
        self.assertEqual(services.carryover_account(self.game).balance, 2_000_00)
        self.assertEqual(verify_ledger_integrity(), [])

        # Leftover feeds the next batch's pool.
        self.draw = open_spin_batch(self.game)
        self.play([5, 6, 7, 8])
        nxt = self.spin(winning=(80, 81, 82, 83))
        self.assertEqual(nxt.carry_in, 2_000_00)
        self.assertEqual(nxt.prize_pool, 300_00 + 2_000_00)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_grand_prize_is_guaranteed(self):
        winner = self.play([4, 3, 2, 1])
        for _ in range(9):
            self.play([20, 21, 22, 23])
        draw = self.spin()
        # Sales ₦5,000 -> pool ₦3,000; the company tops up ₦97,000 to pay ₦100,000.
        self.assertEqual(draw.grand_winner_count, 1)
        self.assertEqual(draw.house_topup, 97_000_00)
        self.assertEqual(balance(winner), 100_000_00)
        ticket = winner.tickets.get()
        self.assertEqual((ticket.status, ticket.prize_label), (Ticket.Status.WON, "Grand prize"))
        self.assertEqual(verify_ledger_integrity(), [])

    def test_no_grand_winner_whole_pool_goes_to_consolation(self):
        for i in range(30):
            self.play([1, 50 + i % 20, 71 + i % 10, 85])
        draw = self.spin(winning=(1, 2, 3, 4))
        # Sales ₦15,000 -> pool ₦9,000 -> one ₦7,000 consolation, ₦2,000 carried.
        self.assertEqual(draw.consolation_winner_count, 1)
        self.assertEqual(draw.carry_out, 2_000_00)

    def test_tie_break_is_reproducible_from_published_seeds(self):
        for i in range(5):
            self.play([1, 60 + i, 70 + i, 80 + i])  # five players tie on 1 match
        for _ in range(25):
            self.play([20, 21, 22, 23])
        draw = self.spin()  # ₦15,000 sales -> ₦9,000 pool -> exactly one ₦7,000 prize
        self.assertEqual(draw.consolation_winner_count, 1)
        tied = Ticket.objects.filter(draw=draw, match_count=1)
        expected = min(tied, key=lambda t: rng.fair_rank(draw.server_seed, draw.client_seed, t.serial))
        self.assertEqual(Ticket.objects.get(draw=draw, status=Ticket.Status.WON), expected)

    def test_admin_spin_endpoint(self):
        self.play([1, 2, 3, 9])
        close_batch(self.draw)
        player = make_user()
        self.client.force_login(player)
        self.assertEqual(self.client.post(reverse("backoffice:spin", args=[self.draw.pk])).status_code, 302)
        self.client.force_login(self.staff)
        response = self.client.post(reverse("backoffice:spin", args=[self.draw.pk]))
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["numbers"]), 4)
        again = self.client.post(reverse("backoffice:spin", args=[self.draw.pk]))
        self.assertEqual(again.status_code, 400)
        self.assertEqual(self.client.get(reverse("backoffice:spin_queue")).status_code, 200)


class SpinPageTests(TestCase):
    def test_pages_render_open_and_closed(self):
        game = make_spin_game()
        user = make_user(balance=1_000_00)
        self.client.force_login(user)
        for url in ("/", reverse("games:lobby"), reverse("games:game_detail", args=[game.slug])):
            self.assertContains(self.client.get(url), "Next game opens in")
        draw = open_spin_batch(game)
        self.assertContains(self.client.get("/"), "Play Game")
        response = self.client.post(reverse("games:play_game", args=[game.slug]), {"purchase_token": "x1"})
        ticket = user.tickets.get()
        self.assertRedirects(response, reverse("games:ticket_detail", args=[ticket.serial]))
        self.client.post(reverse("games:play_game", args=[game.slug]), {"purchase_token": "x1"})
        self.assertEqual(user.tickets.count(), 1)  # double-click safe
        self.assertContains(self.client.get(reverse("games:ticket_detail", args=[ticket.serial])), "after the spin")
        close_batch(draw)
        services.spin_draw(draw.pk)
        self.assertContains(self.client.get(reverse("games:result_detail", args=[draw.pk])), "Consolation winners")


class AdminControlTests(TestCase):
    """The admin can change price, prizes, times and winner counts without touching code."""

    def setUp(self):
        self.game = make_spin_game()
        self.draw = open_spin_batch(self.game)
        self.admin = User.objects.create_superuser("boss@example.com", "Boss-pass-123")

    def play(self, numbers):
        user = make_user(balance=self.game.ticket_price)
        services.purchase_tickets(user, self.draw.pk, [numbers], quick_pick_flags=[True])
        return user

    def spin(self, **kwargs):
        close_batch(self.draw)
        with mock.patch.object(rng, "derive_numbers", return_value=[1, 2, 3, 4]):
            return services.spin_draw(self.draw.pk, staff_user=self.admin, **kwargs)

    def test_game_editor_uses_naira_and_plain_game_times(self):
        self.client.force_login(self.admin)
        url = f"/admin/games/game/{self.game.pk}/change/"
        form = self.client.get(url).context["adminform"].form
        self.assertEqual(form.initial["ticket_price_naira"], Decimal("500.00"))
        data = {k: v for k, v in form.initial.items() if v is not None and k != "id"}
        data.update({"ticket_price_naira": "750", "grand_prize_naira": "250000", "consolation_prize_naira": "5000",
                     "game_times": "09:00-16:00, 19:00-23:30", "consolation_winners": "25",
                     "is_active": "on"})
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302, response.context and response.context["adminform"].form.errors)
        self.game.refresh_from_db()
        self.assertEqual((self.game.ticket_price, self.game.grand_prize, self.game.consolation_prize),
                         (750_00, 250_000_00, 5_000_00))
        self.assertEqual(self.game.schedule, [["09:00", "16:00"], ["19:00", "23:30"]])
        self.assertEqual(self.game.consolation_winners, 25)
        # New price applies to the next play.
        user = make_user(balance=1_000_00)
        services.play_spin_game(user, self.game.pk)
        self.assertEqual(balance(user), 250_00)

    def test_bad_game_times_rejected(self):
        self.client.force_login(self.admin)
        url = f"/admin/games/game/{self.game.pk}/change/"
        form = self.client.get(url).context["adminform"].form
        data = {k: v for k, v in form.initial.items() if v is not None and k != "id"}
        data["game_times"] = "17:00-08:00"
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("game_times", response.context["adminform"].form.errors)

    def test_fixed_winner_count_more_than_pool_company_pays_difference(self):
        self.game.consolation_winners = 5
        self.game.save()
        for i in range(10):
            self.play([1, 50 + i, 70 + i, 80])
        draw = self.spin()
        # Pool = 10 x 500 x 60% = ₦3,000; 5 x ₦7,000 = ₦35,000 -> company tops up ₦32,000.
        self.assertEqual(draw.consolation_winner_count, 5)
        self.assertEqual(draw.house_topup, 32_000_00)
        self.assertEqual(draw.carry_out, 0)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_fewer_winners_than_pool_carries_the_rest(self):
        self.game.consolation_winners = 1
        self.game.save()
        for i in range(30):
            self.play([1, 50 + i % 20, 71 + i % 9, 85])
        draw = self.spin()  # pool ₦9,000 -> 1 winner ₦7,000 -> ₦2,000 carried
        self.assertEqual((draw.consolation_winner_count, draw.carry_out), (1, 2_000_00))

    def test_spin_time_override_beats_game_setting(self):
        self.game.consolation_winners = 1
        self.game.save()
        for i in range(10):
            self.play([1, 50 + i, 70 + i, 80])
        draw = self.spin(consolation_winners=3)
        self.assertEqual(draw.consolation_target, 3)
        self.assertEqual(draw.consolation_winner_count, 3)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_zero_winners_keeps_pool_for_next_game(self):
        for i in range(30):
            self.play([1, 50 + i % 20, 71 + i % 9, 85])
        draw = self.spin(consolation_winners=0)
        self.assertEqual(draw.consolation_winner_count, 0)
        self.assertEqual(draw.carry_out, 9_000_00)

    def test_spin_endpoint_accepts_winner_count_and_viewer_role_cannot_spin(self):
        from django.contrib.auth.models import Group
        from django.core.management import call_command

        self.play([1, 2, 60, 70])
        close_batch(self.draw)
        call_command("setup_roles", stdout=__import__("io").StringIO())
        viewer = make_user(is_staff=True)
        viewer.groups.add(Group.objects.get(name="Viewer"))
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(reverse("backoffice:spin", args=[self.draw.pk])).status_code, 403)
        self.client.force_login(self.admin)
        with mock.patch.object(rng, "derive_numbers", return_value=[1, 2, 3, 4]):
            data = self.client.post(reverse("backoffice:spin", args=[self.draw.pk]), {"winners": "1"}).json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["consolation_winners"], 1)


class PlayerExperienceTests(TestCase):
    def setUp(self):
        self.game = make_spin_game()
        self.draw = open_spin_batch(self.game)

    def test_play_popup_returns_the_four_numbers(self):
        user = make_user(balance=1_000_00)
        self.client.force_login(user)
        data = self.client.post(reverse("games:play_game", args=[self.game.slug]), {"purchase_token": "p1"},
                                HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["numbers"]), 4)
        self.assertEqual(user.tickets.get().numbers, data["numbers"])

    def test_play_popup_reports_low_balance_with_deposit_link(self):
        user = make_user(balance=100_00)
        self.client.force_login(user)
        data = self.client.post(reverse("games:play_game", args=[self.game.slug]), {"purchase_token": "p2"},
                                HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertFalse(data["ok"])
        self.assertIn("/wallet/deposit/", data["deposit_url"])

    def test_dashboard_shows_details_history_and_kyc(self):
        from apps.accounts.services import submit_kyc

        user = make_user(balance=2_000_00)
        services.play_spin_game(user, self.game.pk)
        submit_kyc(user, id_type="nin", id_number="12345678901", bank_code="058", account_number="0123456789")
        close_batch(self.draw)
        with mock.patch.object(rng, "derive_numbers", return_value=list(user.tickets.get().numbers)):
            services.spin_draw(self.draw.pk)
        self.client.force_login(user)
        page = self.client.get("/")
        for text in ("My details", user.full_name, "KYC status", "Verified", "Name matches ID", "Games played",
                     "Grand prize", "Guaranty Trust Bank"):
            self.assertContains(page, text)

    def test_new_unverified_user_lands_on_dashboard(self):
        user = make_user(tier=User.KycTier.UNVERIFIED)
        user.email_verified = False
        user.save()
        self.client.force_login(user)
        self.assertContains(self.client.get("/"), "Enter code")
