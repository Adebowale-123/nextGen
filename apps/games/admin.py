from decimal import Decimal

from django import forms
from django.contrib import admin

from apps.core.admin_utils import ReadOnlyAdmin, reason_action
from apps.core.money import format_money, to_major, to_minor

from . import services
from .models import Draw, Game, PrizeTier, Ticket


class PrizeTierInline(admin.TabularInline):
    model = PrizeTier
    extra = 0


NAIRA_FIELDS = {"ticket_price": "Price per play (₦)", "grand_prize": "Grand prize (₦)",
                "consolation_prize": "Consolation prize (₦)"}


class GameAdminForm(forms.ModelForm):
    """Edit money in naira (stored in kobo) and game times as plain text."""

    ticket_price_naira = forms.DecimalField(label=NAIRA_FIELDS["ticket_price"], min_value=Decimal("1"), decimal_places=2)
    grand_prize_naira = forms.DecimalField(label=NAIRA_FIELDS["grand_prize"], min_value=0, decimal_places=2)
    consolation_prize_naira = forms.DecimalField(label=NAIRA_FIELDS["consolation_prize"], min_value=0,
                                                 decimal_places=2)
    game_times = forms.CharField(
        label="Game times (daily)", required=False, widget=forms.TextInput(attrs={"size": 40}),
        help_text='Comma-separated open–close times, 24-hour clock. Example: "08:00-17:00, 20:00-24:00".',
    )

    class Meta:
        model = Game
        exclude = ("ticket_price", "grand_prize", "consolation_prize", "schedule")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        game = self.instance
        for field in NAIRA_FIELDS:
            self.initial.setdefault(f"{field}_naira", to_major(getattr(game, field) or 0))
        self.initial.setdefault("game_times", ", ".join(f"{a}-{b}" for a, b in (game.schedule or [])))

    def clean_game_times(self):
        text = self.cleaned_data["game_times"].strip()
        windows = []
        for part in filter(None, (p.strip() for p in text.split(","))):
            try:
                start, end = (x.strip() for x in part.split("-"))
                sh, sm = (int(x) for x in start.split(":"))
                eh, em = (int(x) for x in end.split(":"))
            except ValueError:
                raise forms.ValidationError(f'"{part}" is not like 08:00-17:00.')
            if not (0 <= sh < 24 and 0 <= sm < 60 and 0 <= eh <= 24 and 0 <= em < 60) or (eh, em) <= (sh, sm):
                raise forms.ValidationError(f'"{part}": closing time must be after opening time (max 24:00).')
            windows.append([f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"])
        windows.sort()
        for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
            if next_start < prev_end:
                raise forms.ValidationError("Game times overlap.")
        if self.cleaned_data.get("mode") == Game.Mode.SPIN and not windows:
            raise forms.ValidationError("A spin game needs at least one game time.")
        return windows

    def save(self, commit=True):
        game = super().save(commit=False)
        for field in NAIRA_FIELDS:
            setattr(game, field, to_minor(self.cleaned_data[f"{field}_naira"]))
        game.schedule = self.cleaned_data["game_times"]
        if commit:
            game.save()
            self.save_m2m()
        return game


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    form = GameAdminForm
    list_display = ("name", "mode", "price_display", "times_display", "winners_display", "is_active")
    list_editable = ("is_active",)
    prepopulated_fields = {"slug": ("name",)}
    inlines = [PrizeTierInline]
    fieldsets = (
        (None, {"fields": ("name", "slug", "mode", "tagline", "is_active")}),
        ("Price & times", {"fields": ("ticket_price_naira", "game_times", "currency")}),
        ("Prizes (spin games)", {
            "fields": ("prize_pool_percent", "grand_prize_naira", "consolation_prize_naira", "consolation_winners",
                       "consolation_min_match"),
            "description": "Changes apply to games that have not been spun yet.",
        }),
        ("Numbers", {"fields": ("pick_count", "number_max", "max_tickets_per_draw", "max_lines_per_purchase")}),
        ("Pick games only", {"classes": ("collapse",), "fields": ("draw_time",)}),
    )

    def get_inlines(self, request, obj):
        return [PrizeTierInline] if obj and not obj.is_spin else []

    @admin.display(description="Price")
    def price_display(self, obj):
        return format_money(obj.ticket_price, obj.currency)

    @admin.display(description="Game times")
    def times_display(self, obj):
        return ", ".join(f"{a}–{b}" for a, b in obj.schedule or []) or "—"

    @admin.display(description="Consolation winners / game")
    def winners_display(self, obj):
        if not obj.is_spin:
            return "—"
        return obj.consolation_winners if obj.consolation_winners is not None else "Auto"


def _cancel(modeladmin, request, draw, reason):
    services.cancel_draw(draw.pk, reason, staff_user=request.user)


@admin.register(Draw)
class DrawAdmin(admin.ModelAdmin):
    list_display = ("__str__", "status", "closes_at", "ticket_count", "stake_display", "prizes_display",
                    "winner_count", "winning_numbers")
    list_filter = ("status", "game")
    date_hierarchy = "closes_at"
    readonly_fields = ("game", "draw_number", "status", "opens_at", "server_seed_hash", "revealed_seed",
                       "client_seed", "winning_numbers", "ticket_count", "total_stake", "total_prizes",
                       "winner_count", "locked_at", "drawn_at", "settled_at", "cancel_reason")
    fields = readonly_fields[:4] + ("closes_at",) + readonly_fields[4:]
    actions = ["advance"]

    def get_actions(self, request):
        actions = super().get_actions(request)
        func = reason_action("Cancel draw & refund all tickets", _cancel)
        actions[func.__name__] = (func, func.__name__, func.short_description)
        return actions

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        # Closing time can only be moved while the draw is still selling.
        if obj and obj.status != Draw.Status.OPEN:
            return self.readonly_fields + ("closes_at",)
        return self.readonly_fields

    @admin.display(description="Server seed")
    def revealed_seed(self, obj):
        return obj.public_server_seed or "Hidden until drawn"

    @admin.display(description="Stake")
    def stake_display(self, obj):
        return format_money(obj.total_stake, obj.game.currency)

    @admin.display(description="Prizes")
    def prizes_display(self, obj):
        return format_money(obj.total_prizes, obj.game.currency)

    @admin.action(description="Run scheduler now (lock / draw / settle due draws)")
    def advance(self, request, queryset):
        summary = services.run_scheduler_tick()
        self.message_user(request, f"Scheduler: {summary or 'nothing due'}")


@admin.register(Ticket)
class TicketAdmin(ReadOnlyAdmin):
    list_display = ("serial", "user", "draw", "numbers", "status", "match_count", "prize_display", "purchased_at")
    list_filter = ("status", "draw__game", "is_quick_pick")
    search_fields = ("serial", "user__email", "user__phone", "signature")
    raw_id_fields = ("draw",)

    @admin.display(description="Prize")
    def prize_display(self, obj):
        return format_money(obj.prize_amount, obj.draw.game.currency) if obj.prize_amount else "—"
