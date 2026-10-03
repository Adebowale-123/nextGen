"""Deposits (Flow B) and withdrawals (Flow E)."""

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.tasks import task
from django.urls import reverse
from django.utils import timezone

from apps.core.config import RULES
from apps.compliance import services as compliance
from apps.compliance.models import RiskFlag
from apps.core.money import format_coins, format_money
from apps.ledger.models import JournalTransaction, LedgerAccount
from apps.ledger.services import credit, debit, get_system_account, lock_wallet, post_transaction
from apps.notifications.services import notify

from .models import Deposit, PayoutAccount, WebhookEvent, Withdrawal
from .providers import ProviderError, get_provider, payout_provider

log = logging.getLogger("apps.payments")


class PaymentError(Exception):
    """User-facing payment error."""


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------


def initiate_deposit(user, *, amount, provider_code, channel, currency=None) -> Deposit:
    currency = currency or RULES["DEFAULT_CURRENCY"]
    if provider_code not in settings.PAYMENT_PROVIDERS:
        raise PaymentError("That payment method is not available.")
    provider = get_provider(provider_code)
    if channel not in provider.channels:
        raise PaymentError("That payment channel is not available.")
    try:
        compliance.check_can_deposit(user, amount)
    except compliance.ComplianceError as exc:
        raise PaymentError(str(exc)) from exc

    deposit = Deposit.objects.create(
        user=user, provider=provider_code, channel=channel, amount=amount, currency=currency
    )
    callback_url = settings.SITE_URL + reverse("payments:deposit_return", args=[deposit.reference])
    try:
        url, provider_ref, raw = provider.initialize_deposit(
            deposit,
            email=user.email or f"{user.public_id.hex}@players.nextgen.game",
            phone=user.phone,
            name=user.full_name,
            callback_url=callback_url,
        )
    except ProviderError as exc:
        deposit.status = Deposit.Status.FAILED
        deposit.failure_reason = str(exc)[:255]
        deposit.save(update_fields=["status", "failure_reason"])
        raise PaymentError(str(exc)) from exc
    deposit.checkout_url = url
    deposit.provider_reference = provider_ref
    deposit.raw_response = raw
    deposit.save(update_fields=["checkout_url", "provider_reference", "raw_response"])
    return deposit


def reconcile_deposit(reference) -> Deposit:
    """Ask the provider for the authoritative status and settle the deposit.

    Called from webhooks and the browser return URL. Safe to call repeatedly.
    """
    deposit = Deposit.objects.get(reference=reference)
    if deposit.status != Deposit.Status.PENDING:
        return deposit
    result = get_provider(deposit.provider).verify_deposit(deposit)
    if result.status == "success":
        return _credit_deposit(deposit.pk, result)
    if result.status == "failed":
        return _fail_deposit(deposit.pk, result.raw.get("reason") or result.raw.get("gateway_response") or "Payment failed")
    return deposit


@transaction.atomic
def _credit_deposit(deposit_id, result) -> Deposit:
    deposit = Deposit.objects.select_for_update().select_related("user").get(pk=deposit_id)
    if deposit.status != Deposit.Status.PENDING:
        return deposit
    if result.amount != deposit.amount or result.currency != deposit.currency:
        deposit.status = Deposit.Status.FAILED
        deposit.failure_reason = (
            f"Amount mismatch: expected {deposit.amount} {deposit.currency}, got {result.amount} {result.currency}"
        )
        deposit.save()
        compliance.raise_flag(deposit.user, "deposit_amount_mismatch", deposit.failure_reason,
                              RiskFlag.Severity.HIGH, source=deposit)
        return deposit

    wallet = lock_wallet(deposit.user, deposit.currency)
    clearing = get_system_account(LedgerAccount.Purpose.PROVIDER_CLEARING, deposit.currency, deposit.provider)
    txn = post_transaction(
        JournalTransaction.Type.DEPOSIT,
        [debit(clearing, deposit.amount), credit(wallet.account, deposit.amount)],
        description=f"Deposit via {deposit.provider} ({deposit.get_channel_display()})",
        idempotency_key=f"deposit:{deposit.reference}",
        source=deposit,
    )
    wallet.total_deposited += deposit.amount
    wallet.wagering_remaining += int(deposit.amount * RULES["AML_WAGER_MULTIPLIER"])
    wallet.save(update_fields=["total_deposited", "wagering_remaining"])

    deposit.status = Deposit.Status.SUCCESS
    deposit.ledger_transaction = txn
    deposit.completed_at = timezone.now()
    deposit.raw_response = {**deposit.raw_response, "verification": _jsonable(result.raw)}
    deposit.save()
    notify(deposit.user, "Deposit received",
           f"{format_coins(deposit.amount, with_naira=True)} has been added to your wallet.",
           kind="wallet", link=reverse("payments:wallet"), sms=True)
    return deposit


@transaction.atomic
def _fail_deposit(deposit_id, reason) -> Deposit:
    deposit = Deposit.objects.select_for_update().get(pk=deposit_id)
    if deposit.status == Deposit.Status.PENDING:
        deposit.status = Deposit.Status.FAILED
        deposit.failure_reason = str(reason)[:255]
        deposit.completed_at = timezone.now()
        deposit.save()
    return deposit


def expire_stale_deposits(older_than=timedelta(hours=2)):
    """Re-check old pending deposits once, then mark them failed if still pending."""
    cutoff = timezone.now() - older_than
    count = 0
    for deposit in Deposit.objects.filter(status=Deposit.Status.PENDING, created_at__lt=cutoff):
        try:
            deposit = reconcile_deposit(deposit.reference)
        except ProviderError:
            continue
        if deposit.status == Deposit.Status.PENDING:
            _fail_deposit(deposit.pk, "Payment not completed")
            count += 1
    return count


def _jsonable(data):
    return json.loads(json.dumps(data, default=str))


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def handle_webhook(provider_code, request) -> WebhookEvent:
    provider = get_provider(provider_code)
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        payload = {}
    valid = provider.verify_signature(request)
    event = WebhookEvent.objects.create(provider=provider_code, signature_valid=valid, payload=payload)
    if not valid:
        event.error = "Invalid signature"
        event.save(update_fields=["error"])
        log.warning("Rejected %s webhook with invalid signature", provider_code)
        return event

    parsed = provider.parse_webhook(payload)
    event.event, event.reference = parsed.event[:60], parsed.reference[:100]
    try:
        if parsed.kind == "charge" and Deposit.objects.filter(reference=parsed.reference).exists():
            reconcile_deposit(parsed.reference)
        elif parsed.kind == "payout":
            withdrawal = Withdrawal.objects.filter(reference=parsed.reference).first()
            if withdrawal:
                if parsed.status == "paid":
                    complete_withdrawal(withdrawal.pk)
                elif parsed.status == "failed":
                    fail_withdrawal(withdrawal.pk, f"Provider reported {parsed.event}")
        event.processed = True
    except Exception as exc:  # recorded for ops; provider will retry
        log.exception("Webhook processing failed")
        event.error = str(exc)[:255]
    event.save()
    return event


# ---------------------------------------------------------------------------
# Payout accounts
# ---------------------------------------------------------------------------


def verified_id_name(user):
    """(first, last) name from the player's passed KYC check, if any."""
    from apps.accounts.models import KycSubmission

    kyc = user.kyc_submissions.filter(status=KycSubmission.Status.APPROVED).exclude(id_full_name="").first()
    if kyc is None:
        return None
    parts = kyc.id_full_name.split()
    return (parts[0], " ".join(parts[1:])) if parts else None


def add_payout_account(user, *, bank_code, account_number, enforce_name_match=True) -> PayoutAccount:
    from apps.accounts.kyc_providers import id_name_matches_bank

    provider = payout_provider()
    banks = dict(provider.list_banks())
    if bank_code not in banks:
        raise PaymentError("Select a valid bank.")
    account_number = account_number.strip()
    try:
        account_name = provider.resolve_account(bank_code, account_number, holder_hint=user.full_name)
    except ProviderError as exc:
        raise PaymentError(str(exc)) from exc

    # Once a player has passed KYC, every new bank account must be in the same name as their ID.
    id_name = verified_id_name(user)
    name_verified = False
    if id_name:
        name_verified = id_name_matches_bank(*id_name, account_name)
        if enforce_name_match and RULES["KYC_REQUIRE_BANK_NAME_MATCH"] and not name_verified:
            raise PaymentError(
                f"This account is in the name \"{account_name}\", which doesn't match the name on your ID "
                f"(\"{' '.join(id_name)}\"). Use an account in your own name."
            )

    # Fraud: the same bank account on several player profiles.
    shared = PayoutAccount.objects.filter(bank_code=bank_code, account_number=account_number).exclude(user=user)
    if shared.exists():
        compliance.raise_flag(user, "shared_payout_account",
                              f"Bank account {account_number[-4:]} is registered to {shared.count()} other user(s).",
                              RiskFlag.Severity.HIGH)

    account, created = PayoutAccount.objects.get_or_create(
        user=user, bank_code=bank_code, account_number=account_number,
        defaults={"bank_name": banks[bank_code], "account_name": account_name, "name_verified": name_verified},
    )
    if not created:
        account.is_active = True
        account.account_name = account_name
        account.name_verified = account.name_verified or name_verified
        account.save(update_fields=["is_active", "account_name", "name_verified"])
    return account


# ---------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------


def assess_withdrawal_risk(user, amount, destination, now=None):
    """Soft risk checks: any hit sends the withdrawal to manual review."""
    now = now or timezone.now()
    flags = []
    if user.is_flagged:
        flags.append(f"Account flagged: {user.flag_reason or 'see risk flags'}")
    if user.risk_flags.filter(resolved=False, severity=RiskFlag.Severity.HIGH).exists():
        flags.append("Unresolved high-severity risk flag")
    if amount >= RULES["WITHDRAWAL_REVIEW_THRESHOLD"]:
        flags.append(f"Large withdrawal (≥ {format_money(RULES['WITHDRAWAL_REVIEW_THRESHOLD'])})")
    recent = user.withdrawals.filter(created_at__gte=now - timedelta(hours=24)).exclude(
        status__in=[Withdrawal.Status.CANCELLED, Withdrawal.Status.REJECTED]
    ).count()
    if recent >= RULES["MAX_WITHDRAWALS_PER_DAY"]:
        flags.append(f"Velocity: {recent} withdrawals in the last 24h")
    if user.date_joined > now - timedelta(hours=RULES["NEW_ACCOUNT_REVIEW_HOURS"]):
        flags.append("New account (< 24h old)")
    if PayoutAccount.objects.filter(bank_code=destination.bank_code, account_number=destination.account_number) \
            .exclude(user=user).exists():
        flags.append("Payout account shared with another player")
    return flags


def request_withdrawal(user, *, amount, destination_id, currency=None) -> Withdrawal:
    currency = currency or RULES["DEFAULT_CURRENCY"]
    # Hard checks: these reject the request outright.
    try:
        compliance.ensure_account_active(user)
    except compliance.ComplianceError as exc:
        raise PaymentError(str(exc)) from exc
    if not user.can_withdraw_tier:
        raise PaymentError("Complete Tier 2 ID verification before withdrawing.")
    if amount < RULES["MIN_WITHDRAWAL"]:
        raise PaymentError(f"Minimum withdrawal is {format_coins(RULES['MIN_WITHDRAWAL'], with_naira=True)}.")
    destination = PayoutAccount.objects.filter(pk=destination_id, user=user, is_active=True).first()
    if destination is None:
        raise PaymentError("Select one of your payout accounts.")

    with transaction.atomic():
        wallet = lock_wallet(user, currency)
        if wallet.account.balance < amount:
            raise PaymentError("Your balance does not cover this withdrawal.")
        if wallet.wagering_remaining > 0:
            raise PaymentError(
                f"Deposits must be played through before withdrawal. "
                f"Play {format_coins(wallet.wagering_remaining)} more to unlock withdrawals."
            )
        flags = assess_withdrawal_risk(user, amount, destination)
        withdrawal = Withdrawal.objects.create(
            user=user,
            destination=destination,
            amount=amount,
            currency=currency,
            status=Withdrawal.Status.PENDING_REVIEW if flags else Withdrawal.Status.APPROVED,
            risk_flags=flags,
            provider=settings.PAYOUT_PROVIDER,
        )
        pending = get_system_account(LedgerAccount.Purpose.WITHDRAWAL_PENDING, currency)
        post_transaction(
            JournalTransaction.Type.WITHDRAWAL_HOLD,
            [debit(wallet.account, amount), credit(pending, amount)],
            description=f"Withdrawal to {destination}",
            idempotency_key=f"withdrawal-hold:{withdrawal.reference}",
            source=withdrawal,
        )
        if flags:
            notify(user, "Withdrawal under review",
                   f"Your withdrawal of {format_coins(amount, with_naira=True)} is being reviewed. This usually takes a few hours.",
                   kind="wallet")
        else:
            transaction.on_commit(lambda: process_payout_task.enqueue(withdrawal.pk))
    return withdrawal


@task
def process_payout_task(withdrawal_id: int):
    process_payout(withdrawal_id)


def process_payout(withdrawal_id):
    with transaction.atomic():
        withdrawal = Withdrawal.objects.select_for_update().select_related("destination").get(pk=withdrawal_id)
        if withdrawal.status != Withdrawal.Status.APPROVED:
            return withdrawal
        withdrawal.status = Withdrawal.Status.PROCESSING
        withdrawal.save(update_fields=["status"])
    try:
        result = get_provider(withdrawal.provider or settings.PAYOUT_PROVIDER).initiate_payout(withdrawal)
    except ProviderError as exc:
        return fail_withdrawal(withdrawal.pk, str(exc))
    Withdrawal.objects.filter(pk=withdrawal.pk).update(
        provider_reference=result.provider_reference, raw_response=_jsonable(result.raw)
    )
    if result.status == "paid":
        return complete_withdrawal(withdrawal.pk)
    if result.status == "failed":
        return fail_withdrawal(withdrawal.pk, result.reason or "Payout failed")
    return withdrawal  # "processing": the provider webhook will finish it


@transaction.atomic
def complete_withdrawal(withdrawal_id) -> Withdrawal:
    withdrawal = Withdrawal.objects.select_for_update().select_related("user").get(pk=withdrawal_id)
    if withdrawal.status not in (Withdrawal.Status.PROCESSING, Withdrawal.Status.APPROVED):
        return withdrawal
    # Lock order everywhere: wallet first, then system accounts (avoids deadlocks).
    wallet = lock_wallet(withdrawal.user, withdrawal.currency)
    pending = get_system_account(LedgerAccount.Purpose.WITHDRAWAL_PENDING, withdrawal.currency)
    clearing = get_system_account(LedgerAccount.Purpose.PROVIDER_CLEARING, withdrawal.currency, withdrawal.provider)
    post_transaction(
        JournalTransaction.Type.WITHDRAWAL_PAID,
        [debit(pending, withdrawal.amount), credit(clearing, withdrawal.amount)],
        description=f"Payout {withdrawal.reference}",
        idempotency_key=f"withdrawal-paid:{withdrawal.reference}",
        source=withdrawal,
    )
    wallet.total_withdrawn += withdrawal.amount
    wallet.save(update_fields=["total_withdrawn"])
    withdrawal.status = Withdrawal.Status.PAID
    withdrawal.completed_at = timezone.now()
    withdrawal.save(update_fields=["status", "completed_at"])
    notify(withdrawal.user, "Withdrawal sent",
           f"{format_coins(withdrawal.amount, with_naira=True)} is on its way to {withdrawal.destination}.",
           kind="wallet", link=reverse("payments:wallet"), sms=True)
    return withdrawal


def _reverse_hold(withdrawal, new_status, reason, reviewer=None):
    """Return held funds to the player's wallet."""
    pending = get_system_account(LedgerAccount.Purpose.WITHDRAWAL_PENDING, withdrawal.currency)
    wallet = lock_wallet(withdrawal.user, withdrawal.currency)
    post_transaction(
        JournalTransaction.Type.WITHDRAWAL_REVERSAL,
        [debit(pending, withdrawal.amount), credit(wallet.account, withdrawal.amount)],
        description=f"Withdrawal {withdrawal.get_status_display().lower()} → {new_status}: {reason}"[:255],
        idempotency_key=f"withdrawal-reversal:{withdrawal.reference}",
        source=withdrawal,
        created_by=reviewer,
    )
    withdrawal.status = new_status
    withdrawal.completed_at = timezone.now()
    if new_status == Withdrawal.Status.FAILED:
        withdrawal.failure_reason = reason[:255]
    else:
        withdrawal.review_note = reason[:255]
    if reviewer:
        withdrawal.reviewed_by = reviewer
        withdrawal.reviewed_at = timezone.now()
    withdrawal.save()


@transaction.atomic
def fail_withdrawal(withdrawal_id, reason) -> Withdrawal:
    withdrawal = Withdrawal.objects.select_for_update().select_related("user").get(pk=withdrawal_id)
    if withdrawal.status not in (Withdrawal.Status.PROCESSING, Withdrawal.Status.APPROVED):
        return withdrawal
    _reverse_hold(withdrawal, Withdrawal.Status.FAILED, reason)
    notify(withdrawal.user, "Withdrawal failed",
           f"Your withdrawal of {format_coins(withdrawal.amount, with_naira=True)} failed and the funds are back in your wallet.",
           kind="wallet", sms=True)
    return withdrawal


@transaction.atomic
def approve_withdrawal(withdrawal_id, reviewer, note="") -> Withdrawal:
    withdrawal = Withdrawal.objects.select_for_update().get(pk=withdrawal_id)
    if withdrawal.status != Withdrawal.Status.PENDING_REVIEW:
        raise PaymentError("Only withdrawals under review can be approved.")
    withdrawal.status = Withdrawal.Status.APPROVED
    withdrawal.reviewed_by = reviewer
    withdrawal.reviewed_at = timezone.now()
    withdrawal.review_note = note[:255]
    withdrawal.save()
    transaction.on_commit(lambda: process_payout_task.enqueue(withdrawal.pk))
    return withdrawal


@transaction.atomic
def reject_withdrawal(withdrawal_id, reviewer, reason) -> Withdrawal:
    withdrawal = Withdrawal.objects.select_for_update().select_related("user").get(pk=withdrawal_id)
    if withdrawal.status != Withdrawal.Status.PENDING_REVIEW:
        raise PaymentError("Only withdrawals under review can be rejected.")
    _reverse_hold(withdrawal, Withdrawal.Status.REJECTED, reason, reviewer=reviewer)
    notify(withdrawal.user, "Withdrawal declined",
           f"Your withdrawal of {format_coins(withdrawal.amount, with_naira=True)} was declined: {reason}. "
           "The funds are back in your wallet.", kind="wallet", email=True)
    return withdrawal


@transaction.atomic
def cancel_withdrawal(user, reference) -> Withdrawal:
    withdrawal = Withdrawal.objects.select_for_update().select_related("user").filter(
        user=user, reference=reference).first()
    if withdrawal is None or withdrawal.status != Withdrawal.Status.PENDING_REVIEW:
        raise PaymentError("This withdrawal can no longer be cancelled.")
    _reverse_hold(withdrawal, Withdrawal.Status.CANCELLED, "Cancelled by player")
    return withdrawal
