from decimal import Decimal

from django import forms

from apps.core.money import coins_to_minor, to_minor

from .models import Deposit, PayoutAccount
from .providers import deposit_providers, payout_provider


class AmountField(forms.DecimalField):
    def __init__(self, **kwargs):
        kwargs.setdefault("min_value", Decimal("1"))
        kwargs.setdefault("max_digits", 14)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("widget", forms.NumberInput(attrs={"inputmode": "decimal", "step": "0.01",
                                                             "class": "amount-input", "placeholder": "0.00"}))
        super().__init__(**kwargs)

    def clean(self, value):
        return to_minor(super().clean(value))


class CoinsField(forms.IntegerField):
    """Whole coins in the form; kobo in cleaned_data."""

    def __init__(self, **kwargs):
        kwargs.setdefault("min_value", 1)
        kwargs.setdefault("widget", forms.NumberInput(attrs={"inputmode": "numeric", "step": "1",
                                                             "class": "amount-input", "placeholder": "0"}))
        super().__init__(**kwargs)

    def clean(self, value):
        return coins_to_minor(super().clean(value))


class DepositForm(forms.Form):
    amount = CoinsField(label="How many coins?")
    channel = forms.ChoiceField(choices=Deposit.Channel.choices, widget=forms.RadioSelect, initial="card")
    provider = forms.ChoiceField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        providers = deposit_providers()
        self.fields["provider"].choices = [(p.code, p.label) for p in providers]
        self.fields["provider"].initial = providers[0].code if providers else None
        if len(providers) == 1:
            self.fields["provider"].widget = forms.HiddenInput()


class WithdrawForm(forms.Form):
    amount = CoinsField(label="Coins to cash out")
    destination = forms.ModelChoiceField(queryset=PayoutAccount.objects.none(), empty_label=None,
                                         label="Pay to", widget=forms.RadioSelect)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["destination"].queryset = user.payout_accounts.filter(is_active=True)


class PayoutAccountForm(forms.Form):
    bank_code = forms.ChoiceField(label="Bank / wallet provider")
    account_number = forms.CharField(
        max_length=10, min_length=10, label="Account number",
        widget=forms.TextInput(attrs={"inputmode": "numeric", "pattern": r"\d{10}", "placeholder": "10-digit NUBAN"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        banks = sorted(payout_provider().list_banks(), key=lambda b: b[1])
        self.fields["bank_code"].choices = [("", "Select bank")] + banks

    def clean_account_number(self):
        value = self.cleaned_data["account_number"].strip()
        if not value.isdigit():
            raise forms.ValidationError("Account number must be 10 digits.")
        return value
