from django.test import TestCase

from apps.core.models import PlatformSettings
from apps.core.money import coins_to_minor, format_coins
from apps.core.tests.helpers import make_user


class CoinTests(TestCase):
    def test_default_rate_is_ten_naira(self):
        self.assertEqual(coins_to_minor(50), 500_00)
        self.assertEqual(format_coins(500_00), "50 coins")
        self.assertEqual(format_coins(1_000_00, with_naira=True), "100 coins (₦1,000.00)")
        self.assertEqual(format_coins(10_00), "1 coin")
        self.assertEqual(format_coins(100_000_00), "10,000 coins")

    def test_admin_can_change_the_rate(self):
        row = PlatformSettings.load()
        row.coin_value = 5
        row.save()
        self.assertEqual(format_coins(500_00), "100 coins")
        self.assertEqual(coins_to_minor(100), 500_00)

    def test_buy_coins_page_charges_naira_for_coins(self):
        user = make_user()
        self.client.force_login(user)
        page = self.client.get("/wallet/deposit/?coins=100")
        self.assertContains(page, "Buy coins")
        self.assertContains(page, "₦1,000.00")  # 100-coin package price
        self.assertContains(page, "Min 100 coins")
        self.client.post("/wallet/deposit/", {"amount": "100", "channel": "card", "provider": "mock"})
        self.assertEqual(user.deposits.get().amount, 1_000_00)

    def test_player_pages_show_coins_not_prize_pool(self):
        from apps.games.models import Game

        Game.objects.create(name="NextGen Daily", slug="nextgen-daily", ticket_price=500_00, pick_count=4,
                            number_max=90)
        self.client.force_login(make_user(balance=2_000_00))
        page = self.client.get("/")
        for text in ("200 coins", "50 coins", "Grand prize", "₦100,000.00", "₦7,000.00", "Buy coins"):
            self.assertContains(page, text)
        self.assertNotContains(page, "Prize pool")
