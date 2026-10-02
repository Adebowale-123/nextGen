from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def verified_required(view):
    """Logged in AND phone/email verified (KYC Tier 1+)."""

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_contact_verified:
            messages.info(request, "Verify your account to continue.")
            return redirect("accounts:verify")
        return view(request, *args, **kwargs)

    return wrapper
