import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


def normalize_phone(phone: str | None) -> str | None:
    """Normalise Nigerian numbers to E.164 (+234XXXXXXXXXX)."""
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if digits.startswith("+"):
        return digits
    if digits.startswith("234"):
        return "+" + digits
    if digits.startswith("0") and len(digits) == 11:
        return "+234" + digits[1:]
    return "+" + digits


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, phone, password, **extra):
        if not email and not phone:
            raise ValueError("An email address or phone number is required.")
        email = self.normalize_email(email).lower() if email else None
        user = self.model(email=email, phone=normalize_phone(phone), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email=None, phone=None, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, phone, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("email_verified", True)
        return self._create_user(email, None, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    class KycTier(models.IntegerChoices):
        UNVERIFIED = 0, "Unverified"
        BASIC = 1, "Tier 1 · Basic"
        VERIFIED = 2, "Tier 2 · Verified"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        CLOSED = "closed", "Closed"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    email_verified = models.BooleanField(default=False)
    phone_verified = models.BooleanField(default=False)

    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)

    kyc_tier = models.PositiveSmallIntegerField(choices=KycTier.choices, default=KycTier.UNVERIFIED)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    is_flagged = models.BooleanField(default=False, help_text="Flagged by fraud/AML monitoring.")
    flag_reason = models.CharField(max_length=255, blank=True)

    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self):
        return self.email or self.phone or f"user-{self.pk}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or str(self)

    @property
    def display_name(self):
        return self.first_name or str(self)

    @property
    def is_contact_verified(self):
        return self.email_verified or self.phone_verified

    @property
    def can_play(self):
        return self.status == self.Status.ACTIVE and self.kyc_tier >= self.KycTier.BASIC

    @property
    def can_withdraw_tier(self):
        return self.kyc_tier >= self.KycTier.VERIFIED

    def age_on(self, day):
        if not self.date_of_birth:
            return None
        dob = self.date_of_birth
        return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


class OneTimePassword(models.Model):
    class Channel(models.TextChoices):
        SMS = "sms", "SMS"
        EMAIL = "email", "Email"

    class Purpose(models.TextChoices):
        VERIFY_CONTACT = "verify_contact", "Verify contact"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="otps")
    channel = models.CharField(max_length=8, choices=Channel.choices)
    destination = models.CharField(max_length=255)
    purpose = models.CharField(max_length=32, choices=Purpose.choices)
    code_hash = models.CharField(max_length=100)  # "salt$sha256-hex"
    attempts = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at


def kyc_upload_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"kyc/{instance.user.public_id}/{uuid.uuid4().hex}.{ext}"


class KycSubmission(models.Model):
    class IdType(models.TextChoices):
        NIN = "nin", "National Identification Number (NIN)"
        BVN = "bvn", "Bank Verification Number (BVN)"
        PASSPORT = "passport", "International Passport"
        DRIVERS_LICENSE = "drivers_license", "Driver's Licence"
        VOTERS_CARD = "voters_card", "Voter's Card"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Passed"
        REJECTED = "rejected", "Failed"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="kyc_submissions")
    id_type = models.CharField(max_length=20, choices=IdType.choices)
    id_number = models.CharField(max_length=40)
    document = models.FileField(upload_to=kyc_upload_path, blank=True)
    id_full_name = models.CharField(max_length=160, blank=True, help_text="Name on the ID, from the ID registry.")
    bank_account = models.ForeignKey(
        "payments.PayoutAccount", null=True, blank=True, on_delete=models.SET_NULL, related_name="kyc_checks"
    )
    bank_account_name = models.CharField(max_length=160, blank=True, help_text="Name the bank returned.")
    name_match = models.BooleanField(null=True, help_text="Did the ID name match the bank account name?")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    provider = models.CharField(max_length=20, blank=True)
    provider_reference = models.CharField(max_length=100, blank=True)
    provider_response = models.JSONField(default=dict, blank=True)
    auto_decision_note = models.CharField(max_length=255, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)
    reviewed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="kyc_reviews"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} · {self.get_id_type_display()} · {self.status}"

    @property
    def masked_id_number(self):
        return "•" * max(0, len(self.id_number) - 4) + self.id_number[-4:]
