from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.core.decorators import verified_required

from . import oauth, services
from .forms import CompleteProfileForm, KycForm, LoginForm, OtpForm, ProfileForm, RegistrationForm
from .models import KycSubmission, User

BACKEND = "apps.accounts.backends.IdentifierBackend"


def _safe_next(request, default="core:home"):
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure()):
        return target
    return default


def register(request):
    if request.user.is_authenticated:
        return redirect("core:home")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        user = services.register_user(
            email=data.get("email"),
            phone=data.get("phone"),
            password=data["password"],
            first_name=data["first_name"],
            last_name=data["last_name"],
            date_of_birth=data["date_of_birth"],
        )
        login(request, user, backend=BACKEND)
        services.issue_otp(user)
        messages.success(request, "Account created. Enter the 6-digit code we just sent you.")
        return redirect("accounts:verify")
    return render(request, "accounts/register.html", {"form": form, "google_enabled": oauth.is_enabled()})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("core:home")
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        identifier = form.cleaned_data["identifier"]
        if services.is_locked_out(identifier):
            messages.error(request, "Too many failed attempts. Try again in 15 minutes.")
        else:
            user = authenticate(request, identifier=identifier, password=form.cleaned_data["password"])
            if user is None:
                messages.error(request, "Incorrect login details.")
            elif user.status != User.Status.ACTIVE:
                messages.error(request, "This account is not active. Please contact support.")
            else:
                login(request, user, backend=BACKEND)
                if not user.is_contact_verified:
                    return redirect("accounts:verify")
                return redirect(_safe_next(request))
    return render(request, "accounts/login.html", {"form": form, "google_enabled": oauth.is_enabled()})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("core:home")


@login_required
def verify(request):
    user = request.user
    if user.is_contact_verified:
        return redirect("core:home")
    form = OtpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            otp = services.verify_otp(user, form.cleaned_data["code"])
        except services.AccountError as exc:
            form.add_error("code", str(exc))
        else:
            services.mark_contact_verified(user, otp.channel)
            messages.success(request, "You're verified! Welcome aboard.")
            return redirect("core:home")
    destination = user.phone or user.email
    return render(request, "accounts/verify.html", {"form": form, "destination": destination})


@login_required
@require_POST
def resend_otp(request):
    try:
        services.issue_otp(request.user)
        messages.success(request, "A new code is on its way.")
    except services.AccountError as exc:
        messages.error(request, str(exc))
    return redirect("accounts:verify")


@verified_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    locked = request.user.kyc_tier >= User.KycTier.VERIFIED
    if locked:
        for field in form.fields.values():
            field.disabled = True
    if request.method == "POST" and not locked and form.is_valid():
        form.save()
        messages.success(request, "Profile updated.")
        return redirect("accounts:profile")
    return render(
        request,
        "accounts/profile.html",
        {"form": form, "locked": locked, "submissions": request.user.kyc_submissions.all()[:5]},
    )


@verified_required
def kyc(request):
    user = request.user
    pending = user.kyc_submissions.filter(status=KycSubmission.Status.PENDING).first()
    last_failed = user.kyc_submissions.filter(status=KycSubmission.Status.REJECTED).first()
    form = KycForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            submission = services.submit_kyc(
                user,
                id_type=form.cleaned_data["id_type"],
                id_number=form.cleaned_data["id_number"],
                bank_code=form.cleaned_data["bank_code"],
                account_number=form.cleaned_data["account_number"],
                document=form.cleaned_data.get("document"),
            )
        except services.AccountError as exc:
            messages.error(request, str(exc))
        else:
            if submission.status == KycSubmission.Status.APPROVED:
                messages.success(request, "KYC passed ✓ Your ID matches your bank account. Withdrawals are unlocked.")
            elif submission.status == KycSubmission.Status.REJECTED:
                messages.error(request, f"KYC failed: {submission.rejection_reason}.")
                return redirect("accounts:kyc")
            else:
                messages.info(request, "Thanks! Your ID is being checked by our team. We'll notify you shortly.")
            return redirect("core:home")
    return render(request, "accounts/kyc.html", {"form": form, "pending": pending, "last_failed": last_failed})


# --- Google OAuth 2.0 -------------------------------------------------------


def google_start(request):
    if not oauth.is_enabled():
        raise Http404
    return redirect(oauth.authorization_url(request))


def google_callback(request):
    if not oauth.is_enabled():
        raise Http404
    try:
        info = oauth.exchange_code(request, request.GET.get("code"), request.GET.get("state"))
    except oauth.OAuthError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:login")
    user = User.objects.filter(email__iexact=info["email"]).first()
    if user:
        if user.status != User.Status.ACTIVE:
            messages.error(request, "This account is not active. Please contact support.")
            return redirect("accounts:login")
        if not user.email_verified:
            services.mark_contact_verified(user, "email")
        login(request, user, backend=BACKEND)
        return redirect("core:home")
    request.session["google_signup"] = info
    return redirect("accounts:google_complete")


def google_complete(request):
    info = request.session.get("google_signup")
    if not info:
        return redirect("accounts:register")
    form = CompleteProfileForm(request.POST or None, initial=info)
    if request.method == "POST" and form.is_valid():
        if User.objects.filter(email__iexact=info["email"]).exists():
            messages.error(request, "An account with this email already exists. Please sign in.")
            return redirect("accounts:login")
        user = services.register_user(
            email=info["email"],
            first_name=form.cleaned_data["first_name"],
            last_name=form.cleaned_data["last_name"],
            date_of_birth=form.cleaned_data["date_of_birth"],
            email_verified=True,
        )
        del request.session["google_signup"]
        services.grant_welcome_bonus(user)
        login(request, user, backend=BACKEND)
        messages.success(request, "Welcome to NextGen Game!")
        return redirect("core:home")
    return render(request, "accounts/google_complete.html", {"form": form, "email": info["email"]})


# --- Staff-only document access ---------------------------------------------


@staff_member_required
def kyc_document(request, pk):
    submission = get_object_or_404(KycSubmission, pk=pk)
    if not submission.document:
        raise Http404
    return FileResponse(submission.document.open("rb"), as_attachment=False)
