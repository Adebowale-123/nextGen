import re
from datetime import date, timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import KycSubmission, User, normalize_phone
from apps.core.tests.helpers import make_user


class PhoneTests(TestCase):
    def test_normalize_nigerian_numbers(self):
        self.assertEqual(normalize_phone("08031234567"), "+2348031234567")
        self.assertEqual(normalize_phone("2348031234567"), "+2348031234567")
        self.assertEqual(normalize_phone("+234 803 123 4567"), "+2348031234567")


class RegistrationFlowTests(TestCase):
    def setUp(self):
        cache.clear()

    def register(self, identifier="08031234567", dob="1995-05-05"):
        return self.client.post(reverse("accounts:register"), {
            "identifier": identifier, "first_name": "Chidi", "last_name": "Okeke", "date_of_birth": dob,
            "password": "Gr3at-Passw0rd!", "confirm_password": "Gr3at-Passw0rd!", "accept_terms": "on",
        })

    @mock.patch("apps.accounts.services.send_sms")
    def test_phone_registration_otp_and_wallet(self, send_sms):
        response = self.register()
        self.assertRedirects(response, reverse("accounts:verify"))
        user = User.objects.get(phone="+2348031234567")
        self.assertEqual(user.kyc_tier, User.KycTier.UNVERIFIED)
        wallet = user.wallets.get()
        self.assertEqual(wallet.account.balance, 0)

        code = re.search(r"\b(\d{6})\b", send_sms.call_args[0][1]).group(1)
        response = self.client.post(reverse("accounts:verify"), {"code": code})
        self.assertRedirects(response, reverse("core:home"))
        user.refresh_from_db()
        self.assertTrue(user.phone_verified)
        self.assertEqual(user.kyc_tier, User.KycTier.BASIC)

    def test_email_registration_sends_email_otp(self):
        self.register(identifier="new@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("verification code", mail.outbox[0].subject)

    def test_underage_rejected(self):
        dob = (timezone.localdate() - timedelta(days=17 * 365)).isoformat()
        response = self.register(dob=dob)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.exists())

    def test_unverified_user_redirected_from_wallet(self):
        user = make_user(tier=User.KycTier.UNVERIFIED)
        user.email_verified = False
        user.save()
        self.client.force_login(user)
        self.assertRedirects(self.client.get(reverse("payments:wallet")), reverse("accounts:verify"))


class OtpTests(TestCase):
    def setUp(self):
        self.user = make_user()

    @mock.patch("apps.accounts.services.send_email")
    def test_wrong_code_counts_attempts_then_locks(self, _):
        services.issue_otp(self.user)
        for _ in range(5):
            with self.assertRaises(services.AccountError):
                services.verify_otp(self.user, "000000")
        with self.assertRaisesMessage(services.AccountError, "Too many attempts"):
            services.verify_otp(self.user, "123456")

    @mock.patch("apps.accounts.services.send_email")
    def test_expired_code_rejected(self, send_email):
        services.issue_otp(self.user)
        code = re.search(r"\b(\d{6})\b", send_email.call_args[0][2]).group(1)
        self.user.otps.update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaisesMessage(services.AccountError, "expired"):
            services.verify_otp(self.user, code)

    @mock.patch("apps.accounts.services.send_email")
    def test_resend_cooldown(self, _):
        services.issue_otp(self.user)
        with self.assertRaisesMessage(services.AccountError, "wait"):
            services.issue_otp(self.user)


class LoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user(email="login@example.com")

    def test_login_with_email(self):
        response = self.client.post(reverse("accounts:login"),
                                    {"identifier": "LOGIN@example.com", "password": "Sup3r-secret-pass"})
        self.assertRedirects(response, reverse("core:home"))

    def test_lockout_after_repeated_failures(self):
        for _ in range(5):
            self.client.post(reverse("accounts:login"), {"identifier": "login@example.com", "password": "nope"})
        response = self.client.post(reverse("accounts:login"),
                                    {"identifier": "login@example.com", "password": "Sup3r-secret-pass"}, follow=True)
        self.assertContains(response, "Too many failed attempts")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_suspended_user_cannot_log_in(self):
        self.user.status = User.Status.SUSPENDED
        self.user.save()
        response = self.client.post(reverse("accounts:login"),
                                    {"identifier": "login@example.com", "password": "Sup3r-secret-pass"}, follow=True)
        self.assertContains(response, "not active")


class KycTests(TestCase):
    """KYC passes automatically only when the ID exists AND its name matches the bank account name."""

    def submit(self, user, id_number="12345678901", account="0123456789"):
        return services.submit_kyc(user, id_type="nin", id_number=id_number, bank_code="058", account_number=account)

    def test_matching_names_pass_and_unlock_withdrawals(self):
        user = make_user()
        submission = self.submit(user)
        user.refresh_from_db()
        self.assertEqual(submission.status, KycSubmission.Status.APPROVED)
        self.assertTrue(submission.name_match)
        self.assertEqual(user.kyc_tier, User.KycTier.VERIFIED)
        self.assertTrue(submission.bank_account.name_verified)

    def test_bank_name_mismatch_fails_with_reason(self):
        user = make_user()
        submission = self.submit(user, account="0123451111")  # sandbox: account belongs to someone else
        user.refresh_from_db()
        self.assertEqual(submission.status, KycSubmission.Status.REJECTED)
        self.assertFalse(submission.name_match)
        self.assertIn("does not match your bank account name", submission.rejection_reason)
        self.assertEqual(user.kyc_tier, User.KycTier.BASIC)
        # The player can try again with the right account.
        self.assertEqual(self.submit(user).status, KycSubmission.Status.APPROVED)

    def test_unknown_id_fails(self):
        user = make_user()
        submission = self.submit(user, id_number="12345670000")
        self.assertEqual(submission.status, KycSubmission.Status.REJECTED)
        self.assertIn("ID check failed", submission.rejection_reason)

    def test_unknown_bank_account_is_rejected_before_id_check(self):
        user = make_user()
        with self.assertRaisesMessage(services.AccountError, "Bank account"):
            self.submit(user, account="0123450000")
        self.assertFalse(user.kyc_submissions.exists())

    def test_name_rule_can_be_switched_off_by_admin(self):
        from apps.core.models import PlatformSettings

        settings_row = PlatformSettings.load()
        settings_row.kyc_require_bank_name_match = False
        settings_row.save()
        user = make_user()
        self.assertEqual(self.submit(user, account="0123451111").status, KycSubmission.Status.APPROVED)

    def test_after_kyc_new_bank_accounts_must_match_id_name(self):
        from apps.payments.services import PaymentError, add_payout_account

        user = make_user()
        self.submit(user)
        with self.assertRaisesMessage(PaymentError, "doesn't match the name on your ID"):
            add_payout_account(user, bank_code="044", account_number="9999991111")
        account = add_payout_account(user, bank_code="044", account_number="9999992222")
        self.assertTrue(account.name_verified)

    def test_service_outage_goes_to_manual_review_then_admin_approves(self):
        from apps.accounts.kyc_providers import KycResult, MockKycProvider

        user = make_user()
        with mock.patch.object(MockKycProvider, "verify", return_value=KycResult("review", "Service down")):
            submission = self.submit(user)
        self.assertEqual(submission.status, KycSubmission.Status.PENDING)
        with self.assertRaisesMessage(services.AccountError, "under review"):
            self.submit(user)
        staff = User.objects.create_superuser("ops@example.com", "pw-ops-12345")
        services.approve_kyc(submission, reviewer=staff)
        user.refresh_from_db()
        self.assertEqual(user.kyc_tier, User.KycTier.VERIFIED)

    def test_profile_must_be_complete(self):
        user = make_user()
        user.date_of_birth = None
        user.save()
        with self.assertRaisesMessage(services.AccountError, "date of birth"):
            self.submit(user)

    def test_kyc_page_flow(self):
        user = make_user()
        self.client.force_login(user)
        response = self.client.post(reverse("accounts:kyc"), {"id_type": "nin", "id_number": "12345678901",
                                                              "bank_code": "058", "account_number": "0123451111"})
        self.assertRedirects(response, reverse("accounts:kyc"))
        self.assertContains(self.client.get(reverse("core:home")), "Failed")
        response = self.client.post(reverse("accounts:kyc"), {"id_type": "nin", "id_number": "12345678901",
                                                              "bank_code": "058", "account_number": "0123456789"})
        self.assertRedirects(response, reverse("core:home"))
        self.assertContains(self.client.get(reverse("core:home")), "Verified")

    def test_kyc_document_view_is_staff_only(self):
        user = make_user()
        submission = self.submit(user)
        self.client.force_login(user)
        response = self.client.get(reverse("accounts:kyc_document", args=[submission.pk]))
        self.assertEqual(response.status_code, 302)  # bounced to admin login


class AdminSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser("boss@example.com", "Boss-pass-123")

    def test_admin_login_and_lockout(self):
        response = self.client.post("/admin/login/", {"username": "boss@example.com", "password": "Boss-pass-123",
                                                      "next": "/admin/"})
        self.assertRedirects(response, "/admin/")
        self.client.logout()
        for _ in range(5):
            self.client.post("/admin/login/", {"username": "boss@example.com", "password": "wrong"})
        self.client.post("/admin/login/", {"username": "boss@example.com", "password": "Boss-pass-123"})
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_user_record_and_csv_export(self):
        player = make_user(balance=1_000_00)
        self.client.force_login(self.admin)
        page = self.client.get(f"/admin/accounts/user/{player.pk}/change/")
        self.assertContains(page, "Deposits &amp; withdrawals")
        self.assertNotContains(page, "pbkdf2")
        response = self.client.post("/admin/accounts/user/", {"action": "export_csv", "_selected_action": [player.pk]})
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn(player.email, response.content.decode())

    def test_platform_settings_page_and_live_rules(self):
        from apps.core.config import RULES

        self.client.force_login(self.admin)
        response = self.client.get("/admin/core/platformsettings/")
        self.assertRedirects(response, "/admin/core/platformsettings/1/change/")
        form = self.client.get("/admin/core/platformsettings/1/change/").context["adminform"].form
        data = {k: v for k, v in form.initial.items() if v is not None}
        data.update({"min_deposit": "250.00", "kyc_require_bank_name_match": "on"})
        self.client.post("/admin/core/platformsettings/1/change/", data)
        self.assertEqual(RULES["MIN_DEPOSIT"], 250_00)
