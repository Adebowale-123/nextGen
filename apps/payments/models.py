import uuid

from django.conf import settings
from django.db import models


def new_reference(prefix):
    return f"{prefix}{uuid.uuid4().hex}"


def deposit_reference():
    return new_reference("dep_")


def withdrawal_reference():
    return new_reference("wdr_")


class Deposit(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Successful"
        FAILED = "failed", "Failed"

    class Channel(models.TextChoices):
        ANY = "any", "Any method (chosen at checkout)"
        CARD = "card", "Debit card"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        USSD = "ussd", "USSD"
        MOBILE_MONEY = "mobile_money", "Mobile money (OPay, PalmPay…)"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="deposits")
    reference = models.CharField(max_length=64, unique=True, default=deposit_reference)
    provider = models.CharField(max_length=20)
    channel = models.CharField(max_length=20, choices=Channel.choices)
    amount = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    checkout_url = models.URLField(max_length=500, blank=True)
    provider_reference = models.CharField(max_length=100, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    ledger_transaction = models.ForeignKey(
        "ledger.JournalTransaction", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Coin purchase"

    def __str__(self):
        return self.reference


class PayoutAccount(models.Model):
    """A player's registered bank / mobile-money account for withdrawals."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payout_accounts")
    bank_code = models.CharField(max_length=20)
    bank_name = models.CharField(max_length=120)
    account_number = models.CharField(max_length=20)
    account_name = models.CharField(max_length=160)
    provider_recipient_code = models.CharField(max_length=100, blank=True)
    name_verified = models.BooleanField(default=False, help_text="Account name matches the player's verified ID.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "bank_code", "account_number"], name="unique_payout_account"),
        ]

    def __str__(self):
        return f"{self.bank_name} · {self.masked_number} · {self.account_name}"

    @property
    def masked_number(self):
        return "••••" + self.account_number[-4:]


class Withdrawal(models.Model):
    class Status(models.TextChoices):
        PENDING_REVIEW = "pending_review", "Under review"
        APPROVED = "approved", "Approved"
        PROCESSING = "processing", "Processing"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"

    OPEN_STATUSES = (Status.PENDING_REVIEW, Status.APPROVED, Status.PROCESSING)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="withdrawals")
    reference = models.CharField(max_length=64, unique=True, default=withdrawal_reference)
    destination = models.ForeignKey(PayoutAccount, on_delete=models.PROTECT, related_name="withdrawals")
    amount = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=16, choices=Status.choices)
    provider = models.CharField(max_length=20, blank=True)
    provider_reference = models.CharField(max_length=100, blank=True)
    risk_flags = models.JSONField(default=list, blank=True)
    review_note = models.CharField(max_length=255, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.reference


class WebhookEvent(models.Model):
    """Audit log of every inbound provider webhook."""

    provider = models.CharField(max_length=20)
    event = models.CharField(max_length=60, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    signature_valid = models.BooleanField(default=False)
    payload = models.JSONField(default=dict)
    processed = models.BooleanField(default=False)
    error = models.CharField(max_length=255, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
