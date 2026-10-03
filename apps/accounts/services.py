import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.core.config import RULES
from apps.compliance.services import get_settings as get_rg_settings
from apps.core.money import format_coins
from apps.ledger.models import JournalTransaction, LedgerAccount
from apps.ledger.services import create_wallet, credit, debit, ensure_bonus_account, get_system_account, post_transaction
from apps.notifications.services import notify, send_email, send_sms

from .kyc_providers import get_kyc_provider
from .models import KycSubmission, OneTimePassword, User



class AccountError(Exception):
    """User-facing account error."""


# ---------------------------------------------------------------------------
# Registration (Flow A)
# ---------------------------------------------------------------------------


@transaction.atomic
def register_user(*, email=None, phone=None, password=None, first_name="", last_name="", date_of_birth=None,
                  email_verified=False) -> User:
    """Create the user, a 0.00 wallet in each supported currency and default limits."""
    user = User.objects.create_user(
        email=email,
        phone=phone,
        password=password,
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        email_verified=email_verified,
        kyc_tier=User.KycTier.BASIC if email_verified else User.KycTier.UNVERIFIED,
    )
    for currency in RULES["SUPPORTED_CURRENCIES"]:
        create_wallet(user, currency)
    get_rg_settings(user)
    return user


def mark_contact_verified(user, channel):
    if channel == OneTimePassword.Channel.SMS:
        user.phone_verified = True
    else:
        user.email_verified = True
    if user.kyc_tier < User.KycTier.BASIC:
        user.kyc_tier = User.KycTier.BASIC
    user.save(update_fields=["phone_verified", "email_verified", "kyc_tier"])
    notify(user, "Welcome to NextGen Game", "Your account is active. Press Play Game to get your numbers!",
           kind="account")
    grant_welcome_bonus(user)


def grant_welcome_bonus(user):
    """Credit the one-time, play-only welcome bonus (funded by the company share)."""
    amount = RULES["WELCOME_BONUS"]
    key = f"welcome-bonus:{user.public_id}"
    if amount <= 0 or JournalTransaction.objects.filter(idempotency_key=key).exists():
        return None
    currency = RULES["DEFAULT_CURRENCY"]
    wallet = user.wallets.get(currency=currency)
    house = get_system_account(LedgerAccount.Purpose.HOUSE_REVENUE, currency)
    txn = post_transaction(
        JournalTransaction.Type.WELCOME_BONUS,
        [debit(house, amount), credit(ensure_bonus_account(wallet), amount)],
        description="Welcome bonus (play-only)",
        idempotency_key=key,
        source=user,
    )
    notify(user, f"🎁 {format_coins(amount)} welcome bonus",
           f"We've added {format_coins(amount)} bonus to your wallet. Use it to play your first game!",
           kind="wallet")
    return txn


# ---------------------------------------------------------------------------
# OTP
# ---------------------------------------------------------------------------


def _hash_code(otp_id_salt: str, code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"{otp_id_salt}:{code}".encode(), hashlib.sha256).hexdigest()


def primary_channel(user):
    return OneTimePassword.Channel.SMS if user.phone else OneTimePassword.Channel.EMAIL


def issue_otp(user, purpose=OneTimePassword.Purpose.VERIFY_CONTACT, channel=None) -> OneTimePassword:
    channel = channel or primary_channel(user)
    destination = user.phone if channel == OneTimePassword.Channel.SMS else user.email
    latest = user.otps.filter(purpose=purpose).first()
    cooldown = RULES["OTP_RESEND_COOLDOWN_SECONDS"]
    if latest and (timezone.now() - latest.created_at).total_seconds() < cooldown:
        raise AccountError(f"Please wait {cooldown} seconds before requesting another code.")

    # Invalidate any outstanding codes for this purpose.
    user.otps.filter(purpose=purpose, consumed_at__isnull=True).update(consumed_at=timezone.now())
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(8)
    otp = OneTimePassword.objects.create(
        user=user,
        channel=channel,
        destination=destination,
        purpose=purpose,
        code_hash=f"{salt}${_hash_code(salt, code)}",
        expires_at=timezone.now() + timedelta(seconds=RULES["OTP_TTL_SECONDS"]),
    )
    otp.demo_code = code  # only ever shown on screen when DEMO_MODE is on
    message = f"Your NextGen Game code is {code}. It expires in {RULES['OTP_TTL_SECONDS'] // 60} minutes."
    if channel == OneTimePassword.Channel.SMS:
        send_sms(destination, message)
    else:
        send_email(destination, "Your NextGen Game verification code", message)
    return otp


def verify_otp(user, code, purpose=OneTimePassword.Purpose.VERIFY_CONTACT) -> OneTimePassword:
    otp = user.otps.filter(purpose=purpose, consumed_at__isnull=True).first()
    if otp is None or otp.is_expired:
        raise AccountError("That code has expired. Request a new one.")
    if otp.attempts >= RULES["OTP_MAX_ATTEMPTS"]:
        raise AccountError("Too many attempts. Request a new code.")
    otp.attempts += 1
    salt, expected = otp.code_hash.split("$", 1)
    if not hmac.compare_digest(expected, _hash_code(salt, code.strip())):
        otp.save(update_fields=["attempts"])
        left = RULES["OTP_MAX_ATTEMPTS"] - otp.attempts
        raise AccountError(f"Incorrect code. {left} attempt(s) left.")
    otp.consumed_at = timezone.now()
    otp.save(update_fields=["attempts", "consumed_at"])
    return otp


# ---------------------------------------------------------------------------
# Login brute-force protection
# ---------------------------------------------------------------------------


def _lock_key(identifier):
    return f"login-fail:{identifier.strip().lower()}"


def is_locked_out(identifier):
    return cache.get(_lock_key(identifier), 0) >= RULES["LOGIN_MAX_ATTEMPTS"]


def record_login_failure(identifier):
    key = _lock_key(identifier)
    cache.add(key, 0, RULES["LOGIN_LOCKOUT_SECONDS"])
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, RULES["LOGIN_LOCKOUT_SECONDS"])


def clear_login_failures(identifier):
    cache.delete(_lock_key(identifier))


# ---------------------------------------------------------------------------
# KYC (Tier 2)
# ---------------------------------------------------------------------------


def submit_kyc(user, *, id_type, id_number, bank_code, account_number, document=None) -> KycSubmission:
    """Automatic KYC: the ID must exist and the name on it must match the bank account name.

    Passed  -> Tier 2 (withdrawals unlocked) and the bank account is marked verified.
    Failed  -> the player sees the reason and can try again.
    Pending -> only when the ID service can't give an answer (outage / document needing a human).
    """
    from apps.payments.services import PaymentError, add_payout_account

    from .kyc_providers import id_name_matches_bank

    if user.kyc_tier >= User.KycTier.VERIFIED:
        raise AccountError("Your identity is already verified.")
    if user.kyc_submissions.filter(status=KycSubmission.Status.PENDING).exists():
        raise AccountError("You already have a verification under review.")
    if not (user.first_name and user.last_name and user.date_of_birth):
        raise AccountError("Complete your name and date of birth before verifying your ID.")

    # 1. Look up the bank account (this also confirms it exists).
    try:
        bank_account = add_payout_account(user, bank_code=bank_code, account_number=account_number,
                                          enforce_name_match=False)
    except PaymentError as exc:
        raise AccountError(f"Bank account: {exc}") from exc

    # 2. Check the ID with the identity provider.
    provider = get_kyc_provider()
    id_number = id_number.strip().upper()
    result = provider.verify(user, id_type, id_number)
    submission = KycSubmission.objects.create(
        user=user,
        id_type=id_type,
        id_number=id_number,
        document=document or "",
        provider=provider.code,
        provider_reference=result.reference,
        provider_response=result.raw,
        id_full_name=result.full_name,
        bank_account=bank_account,
        bank_account_name=bank_account.account_name,
        auto_decision_note=result.note[:255],
    )

    # 3. Decide.
    if result.status == "review":
        notify(user, "Verification under review", "Our team will check your ID shortly.", kind="account")
        return submission
    if result.status == "failed":
        reject_kyc(submission, None, f"ID check failed: {result.note}")
        return submission

    submission.name_match = id_name_matches_bank(result.first_name, result.last_name, bank_account.account_name)
    submission.save(update_fields=["name_match"])
    if RULES["KYC_REQUIRE_BANK_NAME_MATCH"] and not submission.name_match:
        reject_kyc(
            submission, None,
            f'Name on your ID ("{result.full_name}") does not match your bank account name '
            f'("{bank_account.account_name}")',
        )
        return submission
    approve_kyc(submission, reviewer=None)
    return submission


@transaction.atomic
def approve_kyc(submission, reviewer=None):
    submission.status = KycSubmission.Status.APPROVED
    submission.reviewed_by = reviewer
    submission.reviewed_at = timezone.now()
    submission.save()
    user = submission.user
    user.kyc_tier = User.KycTier.VERIFIED
    user.save(update_fields=["kyc_tier"])
    if submission.bank_account_id:
        submission.bank_account.name_verified = True
        submission.bank_account.save(update_fields=["name_verified"])
    notify(user, "KYC passed ✓", "Your ID and bank account are verified. Withdrawals are now unlocked.",
           kind="account", sms=True, email=True)


def reject_kyc(submission, reviewer, reason):
    submission.status = KycSubmission.Status.REJECTED
    submission.reviewed_by = reviewer
    submission.reviewed_at = timezone.now()
    submission.rejection_reason = reason[:255]
    submission.save()
    notify(submission.user, "KYC failed", f"{reason}. Please check your details and try again.",
           kind="account", email=True)
