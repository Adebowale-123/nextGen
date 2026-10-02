import re
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.games.models import Game


class DemoModeTests(TestCase):
    def setUp(self):
        cache.clear()

    def register(self):
        return self.client.post(reverse("accounts:register"), {
            "identifier": "08031230000", "first_name": "Test", "last_name": "Player", "date_of_birth": "1990-01-01",
            "password": "Gr3at-Passw0rd!", "confirm_password": "Gr3at-Passw0rd!", "accept_terms": "on",
        }, follow=True)

    @override_settings(DEMO_MODE=True)
    def test_demo_mode_shows_code_and_banner(self):
        page = self.register()
        self.assertContains(page, "Public demo")
        code = re.search(r"Demo mode: your code is (\d{6})", page.content.decode()).group(1)
        page = self.client.post(reverse("accounts:verify"), {"code": code}, follow=True)
        self.assertContains(page, "My details")

    @mock.patch("apps.accounts.services.send_sms")
    def test_real_mode_never_shows_code(self, _):
        page = self.register()
        self.assertNotContains(page, "Demo mode: your code")
        self.assertNotContains(page, "Public demo")

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz/").content, b"ok")


class RequestDrivenSchedulerTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(SCHEDULER_ON_REQUEST=True)
    def test_requests_advance_the_scheduler_but_throttled(self):
        with mock.patch("apps.games.services.run_scheduler_tick") as tick:
            self.client.get("/healthz/")
            self.client.get("/healthz/")
            self.client.get("/static/css/app.css")
        self.assertEqual(tick.call_count, 1)

    def test_off_by_default(self):
        with mock.patch("apps.games.services.run_scheduler_tick") as tick:
            self.client.get("/healthz/")
        tick.assert_not_called()

    @override_settings(SCHEDULER_ON_REQUEST=True)
    def test_scheduler_error_does_not_break_pages(self):
        with mock.patch("apps.games.services.run_scheduler_tick", side_effect=RuntimeError("boom")):
            self.assertEqual(self.client.get("/healthz/").status_code, 200)


class AdminAccountPagesTests(TestCase):
    """Regression: an admin created outside sign-up had no wallet and the home page crashed (500)."""

    def test_new_superuser_gets_a_wallet(self):
        from apps.accounts.models import User

        admin = User.objects.create_superuser("owner@example.com", "Owner-pass-123")
        self.assertEqual(admin.wallets.count(), 1)

    def test_old_account_without_wallet_can_browse(self):
        from apps.accounts.models import User
        from apps.ledger.models import LedgerAccount, Wallet

        admin = User.objects.create_superuser("legacy@example.com", "Legacy-pass-123")
        wallet = admin.wallets.get()
        accounts = [wallet.account_id, wallet.bonus_account_id]
        Wallet.objects.filter(pk=wallet.pk).delete()  # simulate an account created before the fix
        LedgerAccount.objects.filter(pk__in=accounts).delete()
        self.client.force_login(admin)
        for url in ("/", "/accounts/register/", "/wallet/", "/wallet/deposit/", "/play/"):
            with self.subTest(url):
                self.assertEqual(self.client.get(url, follow=True).status_code, 200)
        self.assertEqual(admin.wallets.count(), 1)
