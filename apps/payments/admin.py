from django.contrib import admin, messages

from apps.core.admin_utils import ReadOnlyAdmin, reason_action
from apps.core.money import format_money

from . import services
from .models import Deposit, PayoutAccount, WebhookEvent, Withdrawal


@admin.register(Deposit)
class DepositAdmin(ReadOnlyAdmin):
    list_display = ("reference", "user", "amount_display", "provider", "channel", "status", "created_at")
    list_filter = ("status", "provider", "channel")
    search_fields = ("reference", "provider_reference", "user__email", "user__phone")
    date_hierarchy = "created_at"
    actions = ["reverify"]

    @admin.display(description="Amount", ordering="amount")
    def amount_display(self, obj):
        return format_money(obj.amount, obj.currency)

    @admin.action(description="Re-verify with provider")
    def reverify(self, request, queryset):
        for dep in queryset.filter(status=Deposit.Status.PENDING):
            services.reconcile_deposit(dep.reference)
        self.message_user(request, "Re-verification complete.")


def _reject(modeladmin, request, withdrawal, reason):
    services.reject_withdrawal(withdrawal.pk, request.user, reason)


@admin.register(Withdrawal)
class WithdrawalAdmin(ReadOnlyAdmin):
    list_display = ("reference", "user", "amount_display", "destination", "status", "flags", "created_at")
    list_filter = ("status", "provider")
    search_fields = ("reference", "provider_reference", "user__email", "user__phone")
    date_hierarchy = "created_at"
    actions = ["approve"]

    def get_actions(self, request):
        actions = super().get_actions(request)
        func = reason_action("Reject withdrawal (refund to wallet)", _reject)
        actions[func.__name__] = (func, func.__name__, func.short_description)
        return actions

    @admin.display(description="Amount", ordering="amount")
    def amount_display(self, obj):
        return format_money(obj.amount, obj.currency)

    @admin.display(description="Risk flags")
    def flags(self, obj):
        return "; ".join(obj.risk_flags) or "—"

    @admin.action(description="Approve & pay out")
    def approve(self, request, queryset):
        done = 0
        for w in queryset.filter(status=Withdrawal.Status.PENDING_REVIEW):
            services.approve_withdrawal(w.pk, request.user)
            done += 1
        self.message_user(request, f"Approved {done} withdrawal(s).", messages.SUCCESS)


@admin.register(PayoutAccount)
class PayoutAccountAdmin(ReadOnlyAdmin):
    list_display = ("user", "bank_name", "account_number", "account_name", "is_active", "created_at")
    search_fields = ("account_number", "account_name", "user__email", "user__phone")


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyAdmin):
    list_display = ("received_at", "provider", "event", "reference", "signature_valid", "processed", "error")
    list_filter = ("provider", "signature_valid", "processed")
    search_fields = ("reference",)
