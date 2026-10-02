from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.compliance import services
from apps.core.tests.helpers import make_open_draw, make_user
from apps.games import services as games
from apps.payments import services as payments


class ResponsibleGamingTests(TestCase):
    def setUp(self):
        self.user = make_user(balance=5_000_00)

    def test_self_exclusion_blocks_deposits_and_play(self):
        services.self_exclude(self.user, 7)
        with self.assertRaisesMessage(payments.PaymentError, "self-excluded"):
            payments.initiate_deposit(self.user, amount=1_000_00, provider_code="mock", channel="card")
        with self.assertRaisesMessage(games.GameError, "self-excluded"):
            games.purchase_tickets(self.user, make_open_draw().pk, [[1, 2, 3, 4, 5]])

    def test_self_exclusion_cannot_be_shortened(self):
        services.self_exclude(self.user, 30)
        until = services.get_settings(self.user).self_excluded_until
        services.self_exclude(self.user, 1)
        self.assertEqual(services.get_settings(self.user).self_excluded_until, until)

    def test_limit_decrease_immediate_increase_delayed(self):
        _, delayed = services.update_limits(self.user, deposit_limit=10_000_00, stake_limit=None)
        self.assertFalse(delayed)
        self.assertEqual(services.effective_deposit_limit(self.user), 10_000_00)

        _, delayed = services.update_limits(self.user, deposit_limit=40_000_00, stake_limit=None)
        self.assertTrue(delayed)
        self.assertEqual(services.effective_deposit_limit(self.user), 10_000_00)

        rg = services.get_settings(self.user)
        rg.pending_effective_at = timezone.now() - timedelta(seconds=1)
        rg.save()
        self.assertEqual(services.effective_deposit_limit(self.user), 40_000_00)

    def test_tier_cap_still_applies_over_personal_limit(self):
        services.update_limits(self.user, deposit_limit=None, stake_limit=None)
        self.assertEqual(services.effective_deposit_limit(self.user), 50_000_00)  # Tier 1 cap

    def test_daily_stake_limit(self):
        services.update_limits(self.user, deposit_limit=None, stake_limit=150_00)
        draw = make_open_draw()
        games.purchase_tickets(self.user, draw.pk, [[1, 2, 3, 4, 5]])
        with self.assertRaisesMessage(games.GameError, "daily play limit"):
            games.purchase_tickets(self.user, draw.pk, [[6, 7, 8, 9, 10]])
