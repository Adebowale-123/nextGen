from django.contrib import admin

from apps.core.admin_utils import ReadOnlyAdmin
from apps.core.money import format_money

from .models import Entry, JournalTransaction, LedgerAccount, Wallet


class EntryInline(admin.TabularInline):
    model = Entry
    extra = 0
    can_delete = False
    fields = ("account", "direction", "amount_display", "balance_after_display")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Amount")
    def amount_display(self, obj):
        return format_money(obj.amount, obj.account.currency)

    @admin.display(description="Balance after")
    def balance_after_display(self, obj):
        return format_money(obj.balance_after, obj.account.currency)


@admin.register(JournalTransaction)
class JournalTransactionAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "tx_type", "description", "currency", "source_type", "source_id", "reference")
    list_filter = ("tx_type", "currency", "source_type")
    search_fields = ("reference", "description", "idempotency_key", "source_id")
    date_hierarchy = "created_at"
    inlines = [EntryInline]


@admin.register(LedgerAccount)
class LedgerAccountAdmin(ReadOnlyAdmin):
    list_display = ("code", "name", "kind", "purpose", "balance_display")
    list_filter = ("purpose", "kind", "currency")
    search_fields = ("code", "name", "user__email", "user__phone")

    @admin.display(description="Balance", ordering="balance")
    def balance_display(self, obj):
        return format_money(obj.balance, obj.currency)


@admin.register(Wallet)
class WalletAdmin(ReadOnlyAdmin):
    list_display = ("user", "currency", "balance_display", "wagering_display", "total_deposited", "total_won")
    search_fields = ("user__email", "user__phone")

    @admin.display(description="Balance")
    def balance_display(self, obj):
        return format_money(obj.balance, obj.currency)

    @admin.display(description="Wagering left")
    def wagering_display(self, obj):
        return format_money(obj.wagering_remaining, obj.currency)
