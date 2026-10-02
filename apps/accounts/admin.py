import csv
import logging

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserCreationForm
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from apps.core.admin_utils import reason_action
from apps.core.money import format_money

from . import services
from .models import KycSubmission, User

audit = logging.getLogger("apps.audit")


class KycInline(admin.TabularInline):
    model = KycSubmission
    fk_name = "user"
    extra = 0
    can_delete = False
    fields = ("created_at", "id_type", "masked_id_number", "id_full_name", "bank_account_name", "name_match",
              "status", "rejection_reason")
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class StaffUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("email", "first_name", "last_name")


def _flag(modeladmin, request, user, reason):
    user.is_flagged = True
    user.flag_reason = reason[:255]
    user.save(update_fields=["is_flagged", "flag_reason"])


def _suspend(modeladmin, request, user, reason):
    user.status = User.Status.SUSPENDED
    user.flag_reason = reason[:255]
    user.save(update_fields=["status", "flag_reason"])


class KycStatusFilter(admin.SimpleListFilter):
    title = "KYC status"
    parameter_name = "kyc"

    def lookups(self, request, model_admin):
        return [("passed", "Passed"), ("failed", "Failed"), ("pending", "Being checked"), ("none", "Not started")]

    def queryset(self, request, qs):
        if self.value() == "passed":
            return qs.filter(kyc_tier=User.KycTier.VERIFIED)
        if self.value() == "failed":
            return qs.filter(kyc_tier__lt=User.KycTier.VERIFIED,
                             kyc_submissions__status=KycSubmission.Status.REJECTED).distinct()
        if self.value() == "pending":
            return qs.filter(kyc_submissions__status=KycSubmission.Status.PENDING).distinct()
        if self.value() == "none":
            return qs.filter(kyc_submissions__isnull=True, kyc_tier__lt=User.KycTier.VERIFIED)
        return qs


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Every player's full record in one place. Visible to staff only; changes are audit-logged."""

    add_form = StaffUserCreationForm
    add_fieldsets = ((None, {"classes": ("wide",),
                             "fields": ("email", "first_name", "last_name", "password1", "password2")}),)
    ordering = ("-date_joined",)
    list_display = ("__str__", "full_name", "phone", "kyc_badge", "status", "is_flagged", "wallet_balance",
                    "games_played", "date_joined")
    list_filter = (KycStatusFilter, "status", "is_flagged", "is_staff", "email_verified", "phone_verified")
    search_fields = ("email", "phone", "first_name", "last_name", "public_id", "payout_accounts__account_number")
    readonly_fields = ("public_id", "date_joined", "last_login", "wallet_summary", "games_summary",
                       "payments_summary", "bank_summary")
    fieldsets = (
        ("Contact & login", {"fields": ("public_id", "email", "email_verified", "phone", "phone_verified",
                                        "password")}),
        ("Personal details", {"fields": ("first_name", "last_name", "date_of_birth")}),
        ("Account status", {"fields": ("kyc_tier", "status", "is_flagged", "flag_reason")}),
        ("Wallet", {"fields": ("wallet_summary",)}),
        ("Games", {"fields": ("games_summary",)}),
        ("Deposits & withdrawals", {"fields": ("payments_summary",)}),
        ("Bank accounts", {"fields": ("bank_summary",)}),
        ("Staff access", {"classes": ("collapse",), "fields": ("is_active", "is_staff", "is_superuser", "groups",
                                                                "user_permissions", "last_login", "date_joined")}),
    )
    inlines = [KycInline]
    actions = ["unflag", "reactivate", "export_csv"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n_tickets=Count("tickets", distinct=True))

    def get_actions(self, request):
        actions = super().get_actions(request)
        for func in (reason_action("Flag for fraud review", _flag), reason_action("Suspend account", _suspend)):
            actions[func.__name__] = (func, func.__name__, func.short_description)
        if not request.user.is_superuser:
            actions.pop("export_csv", None)
        return actions

    # --- list columns -------------------------------------------------------

    @admin.display(description="KYC")
    def kyc_badge(self, obj):
        if obj.kyc_tier >= User.KycTier.VERIFIED:
            return "✅ Passed"
        latest = obj.kyc_submissions.first()
        if latest is None:
            return "— Not started"
        return {"pending": "⏳ Checking", "rejected": "❌ Failed"}.get(latest.status, latest.get_status_display())

    @admin.display(description="Balance")
    def wallet_balance(self, obj):
        wallet = obj.wallets.select_related("account").first()
        return format_money(wallet.balance, wallet.currency) if wallet else "—"

    @admin.display(description="Games", ordering="n_tickets")
    def games_played(self, obj):
        return obj.n_tickets

    # --- detail sections ----------------------------------------------------

    @admin.display(description="Wallet")
    def wallet_summary(self, obj):
        rows = [
            (w.currency, format_money(w.balance, w.currency), format_money(w.bonus_balance, w.currency),
             format_money(w.total_deposited, w.currency), format_money(w.total_staked, w.currency),
             format_money(w.total_won, w.currency), format_money(w.total_withdrawn, w.currency),
             format_money(w.wagering_remaining, w.currency))
            for w in obj.wallets.select_related("account", "bonus_account")
        ]
        if not rows:
            return "—"
        head = "<tr><th>Currency</th><th>Cash</th><th>Bonus</th><th>Deposited</th><th>Played</th><th>Won</th>" \
               "<th>Withdrawn</th><th>Play-through left</th></tr>"
        body = format_html_join("", "<tr>" + "<td>{}</td>" * 8 + "</tr>", rows)
        return format_html('<table class="mini">{}{}</table>', mark_safe(head), body)

    @admin.display(description="Games")
    def games_summary(self, obj):
        tickets = obj.tickets.select_related("draw__game").order_by("-purchased_at")[:10]
        totals = obj.tickets.aggregate(n=Count("id"), won=Count("id", filter=Q(status="won")),
                                       prize=Sum("prize_amount"))
        rows = [(t.purchased_at.strftime("%d %b %Y %H:%M"), t.draw, ", ".join(map(str, t.numbers)),
                 t.get_status_display(), format_money(t.prize_amount) if t.prize_amount else "—") for t in tickets]
        link = reverse("admin:games_ticket_changelist") + f"?user__id__exact={obj.pk}"
        return format_html(
            '<p>{} played · {} won · {} total winnings · <a href="{}">see all tickets</a></p>'
            '<table class="mini"><tr><th>When</th><th>Game</th><th>Numbers</th><th>Result</th><th>Prize</th></tr>{}</table>',
            totals["n"], totals["won"], format_money(totals["prize"] or 0), link,
            format_html_join("", "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>", rows),
        )

    @admin.display(description="Deposits & withdrawals")
    def payments_summary(self, obj):
        dep = obj.deposits.filter(status="success").aggregate(n=Count("id"), total=Sum("amount"))
        wd = obj.withdrawals.filter(status="paid").aggregate(n=Count("id"), total=Sum("amount"))
        return format_html(
            '{} successful deposits ({}) · <a href="{}">view deposits</a><br>'
            '{} paid withdrawals ({}) · <a href="{}">view withdrawals</a>',
            dep["n"], format_money(dep["total"] or 0), reverse("admin:payments_deposit_changelist") + f"?user__id__exact={obj.pk}",
            wd["n"], format_money(wd["total"] or 0), reverse("admin:payments_withdrawal_changelist") + f"?user__id__exact={obj.pk}",
        )

    @admin.display(description="Bank accounts")
    def bank_summary(self, obj):
        rows = [(a.bank_name, a.account_number, a.account_name, "✅ matches ID" if a.name_verified else "—",
                 "active" if a.is_active else "removed") for a in obj.payout_accounts.all()]
        if not rows:
            return "—"
        return format_html('<table class="mini"><tr><th>Bank</th><th>Number</th><th>Name at bank</th>'
                           '<th>Name check</th><th>Status</th></tr>{}</table>',
                           format_html_join("", "<tr>" + "<td>{}</td>" * 5 + "</tr>", rows))

    # --- actions ------------------------------------------------------------

    @admin.action(description="Clear fraud flag")
    def unflag(self, request, queryset):
        queryset.update(is_flagged=False, flag_reason="")

    @admin.action(description="Reactivate account")
    def reactivate(self, request, queryset):
        queryset.update(status=User.Status.ACTIVE)

    @admin.action(description="Export selected users to CSV (superusers only)")
    def export_csv(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, "Only superusers can export user data.", messages.ERROR)
            return None
        audit.warning("User export by %s: %s users", request.user, queryset.count())
        response = HttpResponse(content_type="text/csv")
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
        response["Content-Disposition"] = f'attachment; filename="nextgen-users-{stamp}.csv"'
        writer = csv.writer(response)
        writer.writerow(["user_id", "first_name", "last_name", "email", "phone", "date_of_birth", "kyc",
                         "status", "flagged", "cash_balance", "bonus_balance", "total_deposited", "total_played",
                         "total_won", "total_withdrawn", "games_played", "bank", "account_last4", "joined"])
        for u in queryset.prefetch_related("wallets__account", "wallets__bonus_account", "payout_accounts"):
            w = next(iter(u.wallets.all()), None)
            bank = next((a for a in u.payout_accounts.all() if a.is_active), None)
            writer.writerow([
                u.public_id, u.first_name, u.last_name, u.email or "", u.phone or "", u.date_of_birth or "",
                self.kyc_badge(u), u.status, u.is_flagged,
                w.balance / 100 if w else 0, w.bonus_balance / 100 if w else 0,
                w.total_deposited / 100 if w else 0, w.total_staked / 100 if w else 0,
                w.total_won / 100 if w else 0, w.total_withdrawn / 100 if w else 0, u.n_tickets,
                bank.bank_name if bank else "", bank.account_number[-4:] if bank else "",
                timezone.localtime(u.date_joined).strftime("%Y-%m-%d %H:%M"),
            ])
        return response

def _reject_kyc(modeladmin, request, submission, reason):
    if submission.status != KycSubmission.Status.PENDING:
        raise ValueError("not pending")
    services.reject_kyc(submission, request.user, reason)


@admin.register(KycSubmission)
class KycSubmissionAdmin(admin.ModelAdmin):
    list_display = ("user", "id_type", "masked_id_number", "id_full_name", "bank_account_name", "name_match",
                    "status", "created_at")
    list_filter = ("status", "id_type", "provider")
    search_fields = ("user__email", "user__phone", "user__last_name")
    readonly_fields = ("user", "id_type", "id_number", "id_full_name", "bank_account", "bank_account_name",
                       "name_match", "document_link", "provider", "provider_reference",
                       "provider_response", "auto_decision_note", "status", "rejection_reason", "reviewed_by",
                       "reviewed_at", "created_at", "profile_snapshot")
    exclude = ("document",)
    actions = ["approve"]

    def get_actions(self, request):
        actions = super().get_actions(request)
        func = reason_action("Reject verification", _reject_kyc)
        actions[func.__name__] = (func, func.__name__, func.short_description)
        return actions

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Document")
    def document_link(self, obj):
        if not obj.document:
            return "—"
        return format_html('<a href="{}" target="_blank" rel="noopener">View uploaded ID</a>',
                           reverse("accounts:kyc_document", args=[obj.pk]))

    @admin.display(description="Profile on file")
    def profile_snapshot(self, obj):
        u = obj.user
        return f"{u.first_name} {u.last_name} · DOB {u.date_of_birth or '—'}"

    @admin.action(description="Approve verification (Tier 2)")
    def approve(self, request, queryset):
        count = 0
        for submission in queryset.filter(status=KycSubmission.Status.PENDING).select_related("user"):
            services.approve_kyc(submission, reviewer=request.user)
            count += 1
        self.message_user(request, f"Approved {count} submission(s).", messages.SUCCESS)
