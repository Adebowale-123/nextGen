import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.compliance.models import RiskFlag
from apps.core.tests.helpers import balance, fund, make_user
from apps.ledger.models import LedgerAccount
from apps.ledger.services import get_system_account, verify_ledger_integrity
from apps.payments import services
from apps.payments.models import Deposit, PayoutAccount, WebhookEvent, Withdrawal
from apps.payments.providers import ChargeStatus, MockProvider, PaystackProvider, mock_webhook_body


class DepositFlowTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def start_deposit(self, amount="250", channel="card"):  # 250 coins = ₦2,500
        response = self.client.post(reverse("payments:deposit"), {"amount": amount, "channel": channel,
                                                                  "provider": "mock"})
        return Deposit.objects.filter(user=self.user).first(), response

    def test_successful_deposit_end_to_end(self):
        deposit, response = self.start_deposit()
        self.assertRedirects(response, deposit.checkout_url, fetch_redirect_response=False)
        self.assertEqual(deposit.amount, 250_000)

        response = self.client.post(reverse("payments:mock_checkout", args=[deposit.reference]), {"outcome": "success"})
        self.assertRedirects(response, reverse("payments:deposit_return", args=[deposit.reference]))
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, Deposit.Status.SUCCESS)
        self.assertEqual(balance(self.user), 250_000)
        wallet = self.user.wallets.get()
        self.assertEqual(wallet.wagering_remaining, 250_000)  # AML 1x play-through
        clearing = get_system_account(LedgerAccount.Purpose.PROVIDER_CLEARING, "NGN", "mock")
        self.assertEqual(clearing.balance, 250_000)
        self.assertTrue(WebhookEvent.objects.filter(reference=deposit.reference, signature_valid=True).exists())
        self.assertEqual(verify_ledger_integrity(), [])

    def test_failed_payment_does_not_credit(self):
        deposit, _ = self.start_deposit()
        self.client.post(reverse("payments:mock_checkout", args=[deposit.reference]), {"outcome": "failed"})
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, Deposit.Status.FAILED)
        self.assertEqual(balance(self.user), 0)

    def test_replayed_confirmation_credits_once(self):
        deposit, _ = self.start_deposit()
        self.client.post(reverse("payments:mock_checkout", args=[deposit.reference]), {"outcome": "success"})
        services.reconcile_deposit(deposit.reference)
        body = mock_webhook_body("charge.success", {"reference": deposit.reference})
        services.handle_webhook("mock", SimpleNamespace(body=body, META={MockProvider.SIGNATURE_HEADER: MockProvider.sign(body)}))
        self.assertEqual(balance(self.user), 250_000)

    def test_webhook_with_bad_signature_rejected(self):
        deposit, _ = self.start_deposit()
        body = mock_webhook_body("charge.success", {"reference": deposit.reference})
        response = self.client.post(reverse("payments:webhook", args=["mock"]), body, content_type="application/json",
                                    HTTP_X_MOCK_SIGNATURE="forged")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(balance(self.user), 0)

    def test_amount_mismatch_is_flagged_not_credited(self):
        deposit, _ = self.start_deposit()
        with mock.patch.object(MockProvider, "verify_deposit",
                               return_value=ChargeStatus("success", 100, "NGN", "x")):
            services.reconcile_deposit(deposit.reference)
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, Deposit.Status.FAILED)
        self.assertEqual(balance(self.user), 0)
        self.assertTrue(RiskFlag.objects.filter(user=self.user, code="deposit_amount_mismatch").exists())

    def test_tier1_daily_deposit_limit(self):
        with self.assertRaisesMessage(services.PaymentError, "daily deposit limit"):
            services.initiate_deposit(self.user, amount=60_000_00, provider_code="mock", channel="card")

    def test_minimum_deposit(self):
        with self.assertRaisesMessage(services.PaymentError, "Minimum deposit"):
            services.initiate_deposit(self.user, amount=50_00, provider_code="mock", channel="card")


@override_settings(PAYSTACK_SECRET_KEY="sk_test_abc")
class PaystackSignatureTests(TestCase):
    def test_signature_verification(self):
        body = json.dumps({"event": "charge.success", "data": {"reference": "dep_x"}}).encode()
        good = hmac.new(b"sk_test_abc", body, hashlib.sha512).hexdigest()
        provider = PaystackProvider()
        self.assertTrue(provider.verify_signature(SimpleNamespace(body=body, META={"HTTP_X_PAYSTACK_SIGNATURE": good})))
        self.assertFalse(provider.verify_signature(SimpleNamespace(body=body, META={"HTTP_X_PAYSTACK_SIGNATURE": "bad"})))
        parsed = provider.parse_webhook(json.loads(body))
        self.assertEqual((parsed.kind, parsed.reference), ("charge", "dep_x"))


class WithdrawalTests(TestCase):
    def setUp(self):
        self.user = make_user(tier=User.KycTier.VERIFIED, balance=50_000_00)
        self.account = services.add_payout_account(self.user, bank_code="058", account_number="0123456789")
        self.staff = User.objects.create_superuser("ops@example.com", "ops-pass-123")

    def test_successful_payout_moves_money_through_ledger(self):
        with self.captureOnCommitCallbacks(execute=True):
            w = services.request_withdrawal(self.user, amount=10_000_00, destination_id=self.account.pk)
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.PAID)
        self.assertEqual(balance(self.user), 40_000_00)
        pending = get_system_account(LedgerAccount.Purpose.WITHDRAWAL_PENDING, "NGN")
        self.assertEqual(pending.balance, 0)
        self.assertEqual(self.user.wallets.get().total_withdrawn, 10_000_00)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_tier1_cannot_withdraw(self):
        tier1 = make_user(balance=5_000_00)
        acct = PayoutAccount.objects.create(user=tier1, bank_code="058", bank_name="GTB", account_number="1111111111",
                                            account_name="X")
        with self.assertRaisesMessage(services.PaymentError, "Tier 2"):
            services.request_withdrawal(tier1, amount=2_000_00, destination_id=acct.pk)

    def test_unplayed_deposit_blocks_withdrawal(self):
        self.user.wallets.update(wagering_remaining=5_000_00)
        with self.assertRaisesMessage(services.PaymentError, "played through"):
            services.request_withdrawal(self.user, amount=2_000_00, destination_id=self.account.pk)

    def test_insufficient_balance(self):
        with self.assertRaisesMessage(services.PaymentError, "does not cover"):
            services.request_withdrawal(self.user, amount=60_000_00, destination_id=self.account.pk)

    def test_cannot_use_someone_elses_account(self):
        other = make_user(tier=User.KycTier.VERIFIED)
        with self.assertRaisesMessage(services.PaymentError, "payout accounts"):
            services.request_withdrawal(other, amount=2_000_00, destination_id=self.account.pk)

    def test_flagged_user_goes_to_review_then_reject_refunds(self):
        self.user.is_flagged = True
        self.user.save()
        w = services.request_withdrawal(self.user, amount=5_000_00, destination_id=self.account.pk)
        self.assertEqual(w.status, Withdrawal.Status.PENDING_REVIEW)
        self.assertEqual(balance(self.user), 45_000_00)  # held
        services.reject_withdrawal(w.pk, self.staff, "Source of funds unclear")
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.REJECTED)
        self.assertEqual(balance(self.user), 50_000_00)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_large_withdrawal_reviewed_then_approved_pays(self):
        fund(self.user, 600_000_00)
        w = services.request_withdrawal(self.user, amount=500_000_00, destination_id=self.account.pk)
        self.assertEqual(w.status, Withdrawal.Status.PENDING_REVIEW)
        self.assertTrue(any("Large withdrawal" in f for f in w.risk_flags))
        with self.captureOnCommitCallbacks(execute=True):
            services.approve_withdrawal(w.pk, self.staff)
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.PAID)

    def test_new_account_goes_to_review(self):
        fresh = make_user(tier=User.KycTier.VERIFIED, balance=5_000_00, aged_hours=1)
        acct = services.add_payout_account(fresh, bank_code="058", account_number="2222222222")
        w = services.request_withdrawal(fresh, amount=2_000_00, destination_id=acct.pk)
        self.assertEqual(w.status, Withdrawal.Status.PENDING_REVIEW)

    def test_provider_failure_reverses_hold(self):
        bad = services.add_payout_account(self.user, bank_code="058", account_number="0123459999")
        with self.captureOnCommitCallbacks(execute=True):
            w = services.request_withdrawal(self.user, amount=5_000_00, destination_id=bad.pk)
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.FAILED)
        self.assertEqual(balance(self.user), 50_000_00)
        self.assertEqual(verify_ledger_integrity(), [])

    def test_player_can_cancel_pending_review(self):
        self.user.is_flagged = True
        self.user.save()
        w = services.request_withdrawal(self.user, amount=5_000_00, destination_id=self.account.pk)
        services.cancel_withdrawal(self.user, w.reference)
        self.assertEqual(balance(self.user), 50_000_00)

    def test_shared_bank_account_raises_flag(self):
        other = make_user(tier=User.KycTier.VERIFIED)
        services.add_payout_account(other, bank_code="058", account_number="0123456789")
        self.assertTrue(RiskFlag.objects.filter(user=other, code="shared_payout_account").exists())
