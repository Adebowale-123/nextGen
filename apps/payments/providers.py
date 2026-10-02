"""
Payment provider adapters.

Each adapter normalises a provider's API into the same small interface used by
payments.services. Webhook handling is split in two: `verify_signature`
authenticates the raw request, and `parse_webhook` turns the JSON body into a
WebhookEvent-like dict. The services then *re-verify* the transaction with the
provider's API before moving money: a webhook is a hint, not proof.

Docs to check against when going live (APIs change):
  Paystack:    https://paystack.com/docs/api/
  Flutterwave: https://developer.flutterwave.com/docs
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field

import requests
from django.conf import settings
from django.urls import reverse

from apps.core.money import to_major, to_minor


class ProviderError(Exception):
    pass


@dataclass
class ChargeStatus:
    status: str  # "success" | "failed" | "pending"
    amount: int = 0
    currency: str = ""
    provider_reference: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class PayoutStatus:
    status: str  # "paid" | "failed" | "processing"
    provider_reference: str = ""
    reason: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ParsedWebhook:
    kind: str  # "charge" | "payout" | "ignored"
    event: str
    reference: str
    status: str = ""


NIGERIAN_BANKS = [
    ("044", "Access Bank"), ("023", "Citibank Nigeria"), ("050", "Ecobank Nigeria"), ("070", "Fidelity Bank"),
    ("011", "First Bank of Nigeria"), ("214", "First City Monument Bank"), ("058", "Guaranty Trust Bank"),
    ("030", "Heritage Bank"), ("301", "Jaiz Bank"), ("082", "Keystone Bank"), ("50211", "Kuda Bank"),
    ("999992", "OPay"), ("999991", "PalmPay"), ("526", "Parallex Bank"), ("076", "Polaris Bank"),
    ("101", "Providus Bank"), ("221", "Stanbic IBTC Bank"), ("068", "Standard Chartered Bank"),
    ("232", "Sterling Bank"), ("032", "Union Bank of Nigeria"), ("033", "United Bank for Africa"),
    ("215", "Unity Bank"), ("035", "Wema Bank"), ("057", "Zenith Bank"), ("50515", "Moniepoint MFB"),
]


class BaseProvider:
    code = ""
    label = ""
    channels = []

    def initialize_deposit(self, deposit, *, email, phone, name, callback_url) -> tuple[str, str, dict]:
        """Return (checkout_url, provider_reference, raw)."""
        raise NotImplementedError

    def verify_deposit(self, deposit) -> ChargeStatus:
        raise NotImplementedError

    def verify_signature(self, request) -> bool:
        raise NotImplementedError

    def parse_webhook(self, payload: dict) -> ParsedWebhook:
        raise NotImplementedError

    def list_banks(self):
        return NIGERIAN_BANKS

    def resolve_account(self, bank_code, account_number, holder_hint="") -> str:
        """Return the account holder's name as the bank has it. `holder_hint` is only used by the sandbox."""
        raise NotImplementedError

    def initiate_payout(self, withdrawal) -> PayoutStatus:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Mock gateway (sandbox; no real money)
# ---------------------------------------------------------------------------


class MockProvider(BaseProvider):
    code = "mock"
    label = "Sandbox gateway"
    channels = ["card", "bank_transfer", "ussd", "mobile_money"]
    SIGNATURE_HEADER = "HTTP_X_MOCK_SIGNATURE"

    @staticmethod
    def sign(body: bytes) -> str:
        return hmac.new(settings.MOCK_GATEWAY_SECRET.encode(), body, hashlib.sha256).hexdigest()

    def initialize_deposit(self, deposit, *, email, phone, name, callback_url):
        url = settings.SITE_URL + reverse("payments:mock_checkout", args=[deposit.reference])
        return url, f"mock_{deposit.reference[-12:]}", {"sandbox": True}

    def verify_deposit(self, deposit):
        # The mock gateway's "truth" is what its checkout page recorded.
        outcome = (deposit.raw_response or {}).get("mock_outcome")
        if outcome == "success":
            return ChargeStatus("success", deposit.amount, deposit.currency, deposit.provider_reference)
        if outcome == "failed":
            return ChargeStatus("failed", raw={"reason": "Card declined (sandbox)"})
        return ChargeStatus("pending")

    def verify_signature(self, request):
        signature = request.META.get(self.SIGNATURE_HEADER, "")
        return hmac.compare_digest(signature, self.sign(request.body))

    def parse_webhook(self, payload):
        event = payload.get("event", "")
        data = payload.get("data", {})
        if event.startswith("charge."):
            return ParsedWebhook("charge", event, data.get("reference", ""))
        if event.startswith("transfer."):
            return ParsedWebhook("payout", event, data.get("reference", ""), data.get("status", ""))
        return ParsedWebhook("ignored", event, "")

    SANDBOX_OTHER_NAME = "CHUKWUEMEKA BELLO ADEYEMI"

    def resolve_account(self, bank_code, account_number, holder_hint=""):
        """Sandbox: returns the player's own name, except numbers ending 1111 (someone else's) and 0000 (unknown)."""
        if not (account_number.isdigit() and len(account_number) == 10):
            raise ProviderError("Enter a valid 10-digit account number.")
        if account_number.endswith("0000"):
            raise ProviderError("Account could not be found at this bank.")
        if account_number.endswith("1111"):
            return self.SANDBOX_OTHER_NAME
        return (holder_hint or "SANDBOX ACCOUNT HOLDER").upper()

    def initiate_payout(self, withdrawal):
        # Sandbox: account numbers ending in 9999 fail, everything else pays.
        if withdrawal.destination.account_number.endswith("9999"):
            return PayoutStatus("failed", reason="Beneficiary bank unavailable (sandbox)")
        return PayoutStatus("paid", provider_reference=f"mock_tr_{withdrawal.reference[-10:]}", raw={"sandbox": True})


# ---------------------------------------------------------------------------
# Paystack
# ---------------------------------------------------------------------------


class PaystackProvider(BaseProvider):
    code = "paystack"
    label = "Paystack"
    channels = ["card", "bank_transfer", "ussd", "mobile_money"]
    BASE = "https://api.paystack.co"
    CHANNEL_MAP = {"card": "card", "bank_transfer": "bank_transfer", "ussd": "ussd", "mobile_money": "mobile_money"}

    def _headers(self):
        return {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}

    def _request(self, method, path, **kwargs):
        try:
            response = requests.request(method, self.BASE + path, headers=self._headers(), timeout=20, **kwargs)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError("Paystack is unreachable. Please try again.") from exc
        if not data.get("status"):
            raise ProviderError(data.get("message", "Paystack request failed."))
        return data

    def initialize_deposit(self, deposit, *, email, phone, name, callback_url):
        data = self._request("POST", "/transaction/initialize", json={
            "email": email,
            "amount": deposit.amount,  # kobo
            "currency": deposit.currency,
            "reference": deposit.reference,
            "callback_url": callback_url,
            "channels": [self.CHANNEL_MAP[deposit.channel]],
            "metadata": {"phone": phone, "name": name},
        })
        return data["data"]["authorization_url"], data["data"].get("access_code", ""), data["data"]

    def verify_deposit(self, deposit):
        data = self._request("GET", f"/transaction/verify/{deposit.reference}")["data"]
        status = {"success": "success", "failed": "failed", "abandoned": "failed", "reversed": "failed"}.get(
            data.get("status"), "pending"
        )
        return ChargeStatus(status, int(data.get("amount", 0)), data.get("currency", ""), str(data.get("id", "")), data)

    def verify_signature(self, request):
        signature = request.META.get("HTTP_X_PAYSTACK_SIGNATURE", "")
        expected = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), request.body, hashlib.sha512).hexdigest()
        return bool(settings.PAYSTACK_SECRET_KEY) and hmac.compare_digest(signature, expected)

    def parse_webhook(self, payload):
        event = payload.get("event", "")
        data = payload.get("data", {})
        if event == "charge.success":
            return ParsedWebhook("charge", event, data.get("reference", ""))
        if event in ("transfer.success", "transfer.failed", "transfer.reversed"):
            status = "paid" if event == "transfer.success" else "failed"
            return ParsedWebhook("payout", event, data.get("reference", ""), status)
        return ParsedWebhook("ignored", event, "")

    def list_banks(self):
        try:
            data = self._request("GET", "/bank", params={"country": "nigeria", "perPage": 200})
            return [(b["code"], b["name"]) for b in data["data"] if b.get("active", True)]
        except ProviderError:
            return NIGERIAN_BANKS

    def resolve_account(self, bank_code, account_number, holder_hint=""):
        data = self._request("GET", "/bank/resolve", params={"account_number": account_number, "bank_code": bank_code})
        return data["data"]["account_name"]

    def _recipient_code(self, account):
        if account.provider_recipient_code:
            return account.provider_recipient_code
        data = self._request("POST", "/transferrecipient", json={
            "type": "nuban",
            "name": account.account_name,
            "account_number": account.account_number,
            "bank_code": account.bank_code,
            "currency": "NGN",
        })
        account.provider_recipient_code = data["data"]["recipient_code"]
        account.save(update_fields=["provider_recipient_code"])
        return account.provider_recipient_code

    def initiate_payout(self, withdrawal):
        data = self._request("POST", "/transfer", json={
            "source": "balance",
            "amount": withdrawal.amount,
            "recipient": self._recipient_code(withdrawal.destination),
            "reference": withdrawal.reference,
            "reason": "NextGen Game withdrawal",
        })["data"]
        status = {"success": "paid", "failed": "failed", "reversed": "failed"}.get(data.get("status"), "processing")
        return PayoutStatus(status, data.get("transfer_code", ""), raw=data)


# ---------------------------------------------------------------------------
# Flutterwave (v3 API)
# ---------------------------------------------------------------------------


class FlutterwaveProvider(BaseProvider):
    code = "flutterwave"
    label = "Flutterwave"
    channels = ["card", "bank_transfer", "ussd", "mobile_money"]
    BASE = "https://api.flutterwave.com/v3"
    # Confirm "opay" is enabled on your Flutterwave account for mobile-money wallets.
    CHANNEL_MAP = {"card": "card", "bank_transfer": "banktransfer", "ussd": "ussd", "mobile_money": "opay"}

    def _headers(self):
        return {"Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}", "Content-Type": "application/json"}

    def _request(self, method, path, **kwargs):
        try:
            response = requests.request(method, self.BASE + path, headers=self._headers(), timeout=20, **kwargs)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError("Flutterwave is unreachable. Please try again.") from exc
        if data.get("status") != "success":
            raise ProviderError(data.get("message", "Flutterwave request failed."))
        return data

    def initialize_deposit(self, deposit, *, email, phone, name, callback_url):
        data = self._request("POST", "/payments", json={
            "tx_ref": deposit.reference,
            "amount": str(to_major(deposit.amount)),  # Flutterwave uses major units
            "currency": deposit.currency,
            "redirect_url": callback_url,
            "payment_options": self.CHANNEL_MAP[deposit.channel],
            "customer": {"email": email, "phonenumber": phone or "", "name": name},
            "customizations": {"title": settings.NEXTGEN["BRAND_NAME"]},
        })
        return data["data"]["link"], "", data["data"]

    def verify_deposit(self, deposit):
        try:
            data = self._request("GET", "/transactions/verify_by_reference", params={"tx_ref": deposit.reference})["data"]
        except ProviderError:
            return ChargeStatus("pending")
        status = {"successful": "success", "failed": "failed", "cancelled": "failed"}.get(data.get("status"), "pending")
        return ChargeStatus(status, to_minor(data.get("amount", 0)), data.get("currency", ""), str(data.get("id", "")),
                            data)

    def verify_signature(self, request):
        secret = settings.FLUTTERWAVE_WEBHOOK_HASH
        return bool(secret) and hmac.compare_digest(request.META.get("HTTP_VERIF_HASH", ""), secret)

    def parse_webhook(self, payload):
        event = payload.get("event", "")
        data = payload.get("data", {})
        if event == "charge.completed":
            return ParsedWebhook("charge", event, data.get("tx_ref", ""))
        if event == "transfer.completed":
            status = "paid" if data.get("status") == "SUCCESSFUL" else "failed"
            return ParsedWebhook("payout", event, data.get("reference", ""), status)
        return ParsedWebhook("ignored", event, "")

    def list_banks(self):
        try:
            return [(b["code"], b["name"]) for b in self._request("GET", "/banks/NG")["data"]]
        except ProviderError:
            return NIGERIAN_BANKS

    def resolve_account(self, bank_code, account_number, holder_hint=""):
        data = self._request("POST", "/accounts/resolve",
                             json={"account_number": account_number, "account_bank": bank_code})
        return data["data"]["account_name"]

    def initiate_payout(self, withdrawal):
        dest = withdrawal.destination
        data = self._request("POST", "/transfers", json={
            "account_bank": dest.bank_code,
            "account_number": dest.account_number,
            "amount": float(to_major(withdrawal.amount)),
            "currency": withdrawal.currency,
            "debit_currency": withdrawal.currency,
            "reference": withdrawal.reference,
            "narration": "NextGen Game withdrawal",
        })["data"]
        status = {"SUCCESSFUL": "paid", "FAILED": "failed"}.get(data.get("status"), "processing")
        return PayoutStatus(status, str(data.get("id", "")), raw=data)


PROVIDERS = {p.code: p for p in (MockProvider, PaystackProvider, FlutterwaveProvider)}


def get_provider(code) -> BaseProvider:
    if code not in PROVIDERS:
        raise ProviderError(f"Unknown payment provider: {code}")
    return PROVIDERS[code]()


def deposit_providers():
    return [get_provider(code) for code in settings.PAYMENT_PROVIDERS]


def payout_provider():
    return get_provider(settings.PAYOUT_PROVIDER)


def mock_webhook_body(event, data) -> bytes:
    return json.dumps({"event": event, "data": data}, separators=(",", ":")).encode()
