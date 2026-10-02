from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.decorators import verified_required
from apps.core.money import to_major, to_minor

from . import services

EXCLUSION_PERIODS = [(1, "24 hours"), (7, "7 days"), (30, "30 days"), (180, "6 months"), (365, "1 year")]


class LimitsForm(forms.Form):
    daily_deposit_limit = forms.DecimalField(required=False, min_value=0, decimal_places=2, label="Daily deposit limit (₦)",
                                             help_text="Leave blank for the platform default.")
    daily_stake_limit = forms.DecimalField(required=False, min_value=0, decimal_places=2, label="Daily play limit (₦)",
                                           help_text="Maximum you can spend on tickets per day.")


@verified_required
def settings_view(request):
    rg = services.get_settings(request.user)
    initial = {
        "daily_deposit_limit": to_major(rg.daily_deposit_limit) if rg.daily_deposit_limit is not None else None,
        "daily_stake_limit": to_major(rg.daily_stake_limit) if rg.daily_stake_limit is not None else None,
    }
    form = LimitsForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        dep = form.cleaned_data["daily_deposit_limit"]
        stake = form.cleaned_data["daily_stake_limit"]
        _, delayed = services.update_limits(
            request.user,
            deposit_limit=to_minor(dep) if dep is not None else None,
            stake_limit=to_minor(stake) if stake is not None else None,
        )
        if delayed:
            messages.info(request, "Limit decreases apply now. Increases take effect after a 24-hour cooling-off period.")
        else:
            messages.success(request, "Your limits have been updated.")
        return redirect("compliance:settings")
    return render(request, "compliance/settings.html", {
        "form": form,
        "rg": rg,
        "effective_deposit_limit": services.effective_deposit_limit(request.user),
        "deposited_today": services.deposited_today(request.user),
        "staked_today": services.staked_today(request.user),
        "periods": EXCLUSION_PERIODS,
        "now": timezone.now(),
    })


@verified_required
@require_POST
def self_exclude(request):
    try:
        days = int(request.POST.get("days", 0))
    except ValueError:
        days = 0
    if days not in dict(EXCLUSION_PERIODS) or request.POST.get("confirm") != "yes":
        messages.error(request, "Choose a period and confirm to self-exclude.")
        return redirect("compliance:settings")
    services.self_exclude(request.user, days)
    messages.success(request, f"You're self-excluded for {dict(EXCLUSION_PERIODS)[days]}. "
                              "You can still sign in and withdraw your balance.")
    return redirect("compliance:settings")
