from django.conf import settings
from django.db import models
from django.utils import timezone


class ResponsibleGamingSettings(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rg_settings")
    # Player-chosen caps (minor units). Null means "platform default".
    daily_deposit_limit = models.BigIntegerField(null=True, blank=True)
    daily_stake_limit = models.BigIntegerField(null=True, blank=True)
    # Raising a limit only takes effect after a cooling-off period.
    pending_deposit_limit = models.BigIntegerField(null=True, blank=True)
    pending_stake_limit = models.BigIntegerField(null=True, blank=True)
    pending_effective_at = models.DateTimeField(null=True, blank=True)
    self_excluded_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Responsible gaming settings"
        verbose_name_plural = "Responsible gaming settings"

    def __str__(self):
        return f"RG settings · {self.user}"

    @property
    def is_self_excluded(self):
        return bool(self.self_excluded_until and self.self_excluded_until > timezone.now())


class RiskFlag(models.Model):
    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="risk_flags")
    code = models.CharField(max_length=40)
    detail = models.CharField(max_length=255)
    severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.MEDIUM)
    source_type = models.CharField(max_length=40, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} · {self.user}"
