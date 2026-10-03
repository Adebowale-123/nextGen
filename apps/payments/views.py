from types import SimpleNamespace

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.compliance import services as compliance
from apps.core.config import RULES
from apps.core.money import coin_value, coins_to_minor
from apps.core.decorators import verified_required
from apps.ledger.models import Entry, JournalTransaction
from apps.ledger.services import get_wallet

from . import services
from .forms import DepositForm, PayoutAccountForm, WithdrawForm
from .models import Deposit, Withdrawal
from .providers import PROVIDERS, MockProvider, mock_webhook_body

HISTORY_FILTERS = {
    "all": None,
    "deposits": [JournalTransaction.Type.DEPOSIT, JournalTransaction.Type.WELCOME_BONUS],
    "withdrawals": [JournalTransaction.Type.WITHDRAWAL_HOLD, JournalTransaction.Type.WITHDRAWAL_REVERSAL],
    "tickets": [JournalTransaction.Type.TICKET_PURCHASE, JournalTransaction.Type.TICKET_REFUND],
    "wins": [JournalTransaction.Type.PRIZE_PAYOUT],
}


def coin_packages():
    packages = []
    for part in str(RULES["COIN_PACKAGES"]).split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            packages.append(int(part))
    return packages or [100, 300, 500]


def _wallet(user):
    return get_wallet(user, RULES["DEFAULT_CURRENCY"])


@verified_required
def wallet(request):
    wallet = _wallet(request.user)
    active_filter = request.GET.get("type", "all")
    accounts = [wallet.account_id] + ([wallet.bonus_account_id] if wallet.bonus_account_id else [])
    entries = Entry.objects.filter(account_id__in=accounts).select_related("transaction", "account")
    types = HISTORY_FILTERS.get(active_filter)
    if types:
        entries = entries.filter(transaction__tx_type__in=types)
    page = Paginator(entries, 15).get_page(request.GET.get("page"))
    open_withdrawals = request.user.withdrawals.filter(status__in=Withdrawal.OPEN_STATUSES).select_related("destination")
    return render(request, "payments/wallet.html", {
        "wallet": wallet,
        "page": page,
        "filters": HISTORY_FILTERS.keys(),
        "active_filter": active_filter,
        "open_withdrawals": open_withdrawals,
        "deposit_limit": compliance.effective_deposit_limit(request.user),
        "deposited_today": compliance.deposited_today(request.user),
    })


@verified_required
def deposit(request):
    form = DepositForm(request.POST or None, initial={"amount": request.GET.get("coins")})
    if request.method == "POST" and form.is_valid():
        try:
            dep = services.initiate_deposit(
                request.user,
                amount=form.cleaned_data["amount"],
                provider_code=form.cleaned_data["provider"],
                channel=form.cleaned_data["channel"],
            )
        except services.PaymentError as exc:
            messages.error(request, str(exc))
        else:
            return redirect(dep.checkout_url)
    return render(request, "payments/deposit.html", {
        "form": form,
        "packages": [(c, coins_to_minor(c)) for c in coin_packages()],
        "min_coins": RULES["MIN_DEPOSIT"] // coin_value(),
        "max_coins": RULES["MAX_DEPOSIT"] // coin_value(),
        "coin_value": coin_value(),
        "deposit_limit": compliance.effective_deposit_limit(request.user),
        "deposited_today": compliance.deposited_today(request.user),
        "wallet": _wallet(request.user),
    })


@verified_required
def deposit_return(request, reference):
    """Where the provider sends the player back. We verify server-side; the URL proves nothing."""
    dep = get_object_or_404(Deposit, reference=reference, user=request.user)
    try:
        dep = services.reconcile_deposit(dep.reference)
    except Exception:  # provider hiccup: the webhook / scheduler will settle it
        pass
    return render(request, "payments/deposit_result.html", {"deposit": dep})


@verified_required
def mock_checkout(request, reference):
    """Sandbox stand-in for a hosted checkout page."""
    if "mock" not in settings.PAYMENT_PROVIDERS:
        raise Http404
    dep = get_object_or_404(Deposit, reference=reference, user=request.user, provider="mock")
    if request.method == "POST" and dep.status == Deposit.Status.PENDING:
        outcome = "success" if request.POST.get("outcome") == "success" else "failed"
        Deposit.objects.filter(pk=dep.pk).update(raw_response={**dep.raw_response, "mock_outcome": outcome})
        # Deliver a signed webhook through the same code path a real provider uses.
        body = mock_webhook_body(f"charge.{outcome}", {"reference": dep.reference, "amount": dep.amount})
        fake_request = SimpleNamespace(body=body, META={MockProvider.SIGNATURE_HEADER: MockProvider.sign(body)})
        services.handle_webhook("mock", fake_request)
        return redirect("payments:deposit_return", reference=dep.reference)
    return render(request, "payments/mock_checkout.html", {"deposit": dep})


@csrf_exempt
@require_POST
def webhook(request, provider):
    if provider not in PROVIDERS:
        raise Http404
    event = services.handle_webhook(provider, request)
    if not event.signature_valid:
        return HttpResponse(status=401)
    return HttpResponse("ok")


@verified_required
def withdraw(request):
    user = request.user
    wallet = _wallet(user)
    form = WithdrawForm(request.POST or None, user=user)
    account_form = PayoutAccountForm()
    if request.method == "POST" and form.is_valid():
        try:
            w = services.request_withdrawal(
                user, amount=form.cleaned_data["amount"], destination_id=form.cleaned_data["destination"].pk
            )
        except services.PaymentError as exc:
            messages.error(request, str(exc))
        else:
            w.refresh_from_db()
            if w.status == Withdrawal.Status.PAID:
                messages.success(request, "Withdrawal sent! It should arrive within minutes.")
            elif w.status == Withdrawal.Status.FAILED:
                messages.error(request, f"Payout failed: {w.failure_reason}. Funds returned to your wallet.")
            elif w.status == Withdrawal.Status.PENDING_REVIEW:
                messages.info(request, "Your withdrawal is under review. We'll notify you once it's processed.")
            else:
                messages.info(request, "Withdrawal is processing.")
            return redirect("payments:wallet")
    return render(request, "payments/withdraw.html", {
        "form": form,
        "account_form": account_form,
        "wallet": wallet,
        "min_withdrawal": RULES["MIN_WITHDRAWAL"],
        "recent": user.withdrawals.select_related("destination")[:5],
    })


@verified_required
@require_POST
def add_payout_account(request):
    form = PayoutAccountForm(request.POST)
    if form.is_valid():
        try:
            account = services.add_payout_account(
                request.user, bank_code=form.cleaned_data["bank_code"],
                account_number=form.cleaned_data["account_number"],
            )
            messages.success(request, f"Added {account}.")
        except services.PaymentError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Check the bank and 10-digit account number.")
    return redirect("payments:withdraw")


@verified_required
@require_POST
def cancel_withdrawal(request, reference):
    try:
        services.cancel_withdrawal(request.user, reference)
        messages.success(request, "Withdrawal cancelled. Funds are back in your wallet.")
    except services.PaymentError as exc:
        messages.error(request, str(exc))
    return redirect("payments:wallet")
