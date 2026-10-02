from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.shortcuts import redirect
from django.urls import reverse

from .models import PlatformSettings


@admin.register(PlatformSettings)
class PlatformSettingsAdmin(admin.ModelAdmin):
    """One settings page: clicking it in the admin opens the form directly."""

    readonly_fields = ("updated_at", "updated_by")
    fieldsets = (
        ("Welcome bonus", {"fields": ("welcome_bonus",)}),
        ("Deposits", {"fields": ("min_deposit", "max_deposit", "tier1_daily_deposit_limit",
                                 "default_daily_deposit_limit")}),
        ("Play", {"fields": ("tier1_max_stake_per_purchase", "auto_spin_after_minutes")}),
        ("Withdrawals & anti-money-laundering", {"fields": (
            "min_withdrawal", "withdrawal_review_threshold", "max_withdrawals_per_day",
            "new_account_review_hours", "aml_wager_multiplier")}),
        ("KYC", {"fields": ("kyc_require_bank_name_match",)}),
        ("Responsible gaming", {"fields": ("limit_increase_cooldown_hours",)}),
        ("Last change", {"fields": ("updated_at", "updated_by")}),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = PlatformSettings.load()
        return redirect(reverse("admin:core_platformsettings_change", args=[obj.pk]))

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(LogEntry)
class AuditLogAdmin(admin.ModelAdmin):
    """Who changed what in the admin, and when. Read-only, superusers only."""

    list_display = ("action_time", "user", "content_type", "object_repr", "action_flag", "change_message")
    list_filter = ("action_flag", "content_type")
    search_fields = ("object_repr", "change_message", "user__email")
    date_hierarchy = "action_time"

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
