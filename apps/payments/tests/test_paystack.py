"""Paystack integration, with Paystack's HTTP API simulated (no network, no money)."""

import hashlib
import hmac
import io
import json
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.tests.helpers import coins, make_user
from apps.payments.models import Deposit
from apps.payments.providers import payment_mode, provider_mode

SECRET = "sk_test_unit_secret"
PAYSTACK = dict(PAYSTACK_SECRET_KEY=SECRET, PAYMENT_PROVIDERS=["paystack"], DEMO_MODE=True)


class FakePaystack:
    """Stands in for requests.request against api.paystack.co."""

    def __init__(self, paid_amount=None):
        self.calls = []
        self.paid_amount = paid_amount

    def __call__(self, method, url, headers=None, timeout=None, json=None, params=None):
        self.calls.append((method, url, json))
        assert headers["Authorization"] == f"Bearer {SECRET}"
        response = mock.Mock()
        if url.endswith("/transaction/initialize"):
            response.json.return_value = {"status": True, "data": {
                "authorization_url": "https://checkout.paystack.com/abc123", "access_code": "abc123",
                "reference": json["reference"]}}
        elif "/transaction/verify/" in url:
            response.json.return_value = {"status": True, "data": {
                "status": "success", "amount": self.paid_amount, "currency": "NGN", "id": 987}}
        elif url.endswith("/balance"):
            response.json.return_value = {"status": True, "data": [{"currency": "NGN", "balance": 0}]}
        else:
            response.json.return_value = {"status": False, "message": "unexpected call"}
        return response


@override_settings(**PAYSTACK)
class PaystackCheckoutTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def buy(self, coins_wanted="100"):
        page = self.client.get(reverse("payments:deposit"))
        self.assertContains(page, 'value="paystack"')
        return self.client.post(reverse("payments:deposit"), {"amount": coins_wanted, "provider": "paystack"})

    def signed_webhook(self, payload):
        body = json.dumps(payload).encode()
        signature = hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()
        return self.client.post(reverse("payments:webhook", args=["paystack"]), body,
                                content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE=signature)

    def test_full_purchase_through_paystack(self):
        fake = FakePaystack(paid_amount=1_000_00)
        with mock.patch("apps.payments.providers.requests.request", fake):
            response = self.buy("100")
            # 1. The player is sent to Paystack's real checkout page.
            self.assertRedirects(response, "https://checkout.paystack.com/abc123", fetch_redirect_response=False)
            deposit = Deposit.objects.get()
            method, url, payload = fake.calls[0]
            self.assertEqual(payload["amount"], 1_000_00)  # kobo: 100 coins = ₦1,000
            self.assertEqual(payload["reference"], deposit.reference)
            self.assertNotIn("channels", payload)  # card, transfer and USSD all offered
            self.assertIn(f"/wallet/deposit/{deposit.reference}/return/", payload["callback_url"])

            # 2. Paystack's signed webhook arrives; we re-check with Paystack before crediting.
            self.assertEqual(self.signed_webhook({"event": "charge.success",
                                                  "data": {"reference": deposit.reference}}).status_code, 200)
            self.assertEqual(coins(self.user), 1_000_00)

            # 3. The player returns from Paystack; nothing is credited twice.
            page = self.client.get(reverse("payments:deposit_return", args=[deposit.reference]))
            self.assertContains(page, "Coins added")
            self.assertEqual(coins(self.user), 1_000_00)

    def test_forged_webhook_is_rejected(self):
        with mock.patch("apps.payments.providers.requests.request", FakePaystack(paid_amount=1_000_00)):
            self.buy("100")
            deposit = Deposit.objects.get()
            body = json.dumps({"event": "charge.success", "data": {"reference": deposit.reference}}).encode()
            response = self.client.post(reverse("payments:webhook", args=["paystack"]), body,
                                        content_type="application/json", HTTP_X_PAYSTACK_SIGNATURE="forged")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(coins(self.user), 0)

    def test_underpaid_charge_is_not_credited(self):
        with mock.patch("apps.payments.providers.requests.request", FakePaystack(paid_amount=100_00)):
            self.buy("100")
            deposit = Deposit.objects.get()
            self.client.get(reverse("payments:deposit_return", args=[deposit.reference]))
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, Deposit.Status.FAILED)
        self.assertEqual(coins(self.user), 0)

    def test_banner_says_test_mode(self):
        page = self.client.get("/")
        self.assertContains(page, "Paystack test mode")
        self.assertNotContains(page, "LIVE payments")


class PaymentModeTests(TestCase):
    def test_modes(self):
        with self.settings(PAYSTACK_SECRET_KEY="sk_test_x"):
            self.assertEqual(provider_mode("paystack"), "test")
        with self.settings(PAYSTACK_SECRET_KEY="sk_live_x"):
            self.assertEqual(provider_mode("paystack"), "live")
        self.assertEqual(provider_mode("mock"), "sandbox")
        with self.settings(PAYMENT_PROVIDERS=["mock"]):
            self.assertEqual(payment_mode(), "sandbox")
        with self.settings(PAYMENT_PROVIDERS=["paystack"], PAYSTACK_SECRET_KEY="sk_live_x"):
            self.assertEqual(payment_mode(), "live")

    @override_settings(**PAYSTACK)
    def test_key_check_command(self):
        out = io.StringIO()
        with mock.patch("apps.payments.providers.requests.request", FakePaystack()):
            call_command("check_payment_setup", stdout=out)
        text = out.getvalue()
        self.assertIn("Payment mode: TEST", text)
        self.assertIn("paystack (coin purchases): OK", text)
        self.assertNotIn(SECRET, text)  # never print keys

    @override_settings(PAYSTACK_SECRET_KEY="", PAYMENT_PROVIDERS=["paystack"])
    def test_key_check_reports_missing_key(self):
        out = io.StringIO()
        call_command("check_payment_setup", stdout=out)
        self.assertIn("PAYSTACK_SECRET_KEY is not set", out.getvalue())
