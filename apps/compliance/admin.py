from django.contrib import admin
from django.utils import timezone

from .models import ResponsibleGamingSettings, RiskFlag


@admin.register(RiskFlag)
class RiskFlagAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "code", "severity", "detail", "resolved")
    list_filter = ("resolved", "severity", "code")
    search_fields = ("user__email", "user__phone", "detail")
    readonly_fields = ("user", "code", "detail", "severity", "source_type", "source_id", "created_at",
                       "resolved_by", "resolved_at")
    actions = ["resolve"]

    def has_add_permission(self, request):
        return False

    @admin.action(description="Mark resolved")
    def resolve(self, request, queryset):
        queryset.filter(resolved=False).update(resolved=True, resolved_by=request.user, resolved_at=timezone.now())


@admin.register(ResponsibleGamingSettings)
class ResponsibleGamingSettingsAdmin(admin.ModelAdmin):
    list_display = ("user", "daily_deposit_limit", "daily_stake_limit", "self_excluded_until", "updated_at")
    search_fields = ("user__email", "user__phone")
    readonly_fields = ("user", "updated_at")
