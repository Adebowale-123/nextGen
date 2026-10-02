from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.tests.helpers import make_open_draw, make_user
from apps.games import services as games
from apps.payments import services as payments


class PageSmokeTests(TestCase):
    """Every page renders for the audience it's meant for."""

    @classmethod
    def setUpTestData(cls):
        cls.draw = make_open_draw()
        cls.player = make_user(tier=User.KycTier.VERIFIED, balance=5_000_00)
        games.purchase_tickets(cls.player, cls.draw.pk, [[1, 2, 3, 4, 5]])
        payments.add_payout_account(cls.player, bank_code="058", account_number="0123456789")
        cls.staff = User.objects.create_superuser("ops@example.com", "ops-pass-123")

    def test_public_pages(self):
        for name, args in [("core:home", []), ("core:fairness", []), ("games:lobby", []), ("games:results", []),
                           ("games:draw_detail", [self.draw.pk]), ("accounts:login", []),
                           ("accounts:register", [])]:
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)

    def test_player_pages(self):
        self.client.force_login(self.player)
        ticket = self.player.tickets.first()
        for name, args in [("core:home", []), ("payments:wallet", []), ("payments:deposit", []),
                           ("payments:withdraw", []), ("games:my_tickets", []),
                           ("games:ticket_detail", [ticket.serial]), ("accounts:profile", []),
                           ("accounts:kyc", []), ("compliance:settings", []), ("notifications:inbox", [])]:
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)

    def test_wallet_history_filters(self):
        self.client.force_login(self.player)
        for f in ("all", "deposits", "withdrawals", "tickets", "wins"):
            self.assertEqual(self.client.get(reverse("payments:wallet"), {"type": f}).status_code, 200)

    def test_backoffice_requires_staff(self):
        self.client.force_login(self.player)
        self.assertEqual(self.client.get(reverse("backoffice:dashboard")).status_code, 302)
        self.client.force_login(self.staff)
        for name in ("backoffice:dashboard", "backoffice:report", "backoffice:ledger"):
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(self.client.get(reverse("backoffice:ledger"), {"verify": 1}).status_code, 200)
        csv = self.client.get(reverse("backoffice:report"), {"format": "csv"})
        self.assertEqual(csv["Content-Type"], "text/csv")

    def test_admin_changelists(self):
        self.client.force_login(self.staff)
        for model in ("accounts/user", "accounts/kycsubmission", "ledger/journaltransaction", "ledger/ledgeraccount",
                      "ledger/wallet", "payments/deposit", "payments/withdrawal", "games/draw", "games/ticket",
                      "games/game", "compliance/riskflag"):
            with self.subTest(model):
                self.assertEqual(self.client.get(f"/admin/{model}/").status_code, 200)
