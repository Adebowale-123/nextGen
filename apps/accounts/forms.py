from django import forms
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from .models import KycSubmission, User, normalize_phone


def validate_age(dob):
    today = timezone.localdate()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < settings.NEXTGEN["MIN_AGE"]:
        raise forms.ValidationError(f"You must be {settings.NEXTGEN['MIN_AGE']} or older to play.")


class DateInput(forms.DateInput):
    input_type = "date"


class RegistrationForm(forms.Form):
    identifier = forms.CharField(
        label="Phone number or email",
        max_length=255,
        widget=forms.TextInput(attrs={"placeholder": "0803 000 0000 or you@example.com", "autocomplete": "username"}),
    )
    first_name = forms.CharField(max_length=80)
    last_name = forms.CharField(max_length=80)
    date_of_birth = forms.DateField(widget=DateInput)
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    confirm_password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    accept_terms = forms.BooleanField(
        label="I am 18 or older and accept the Terms and Responsible Gaming policy.",
    )

    def clean_identifier(self):
        value = self.cleaned_data["identifier"].strip()
        if "@" in value:
            value = value.lower()
            forms.EmailField().clean(value)
            if User.objects.filter(email__iexact=value).exists():
                raise forms.ValidationError("An account with this email already exists.")
            self.cleaned_data["email"] = value
        else:
            phone = normalize_phone(value)
            if not phone or not phone[1:].isdigit() or not 11 <= len(phone) <= 15:
                raise forms.ValidationError("Enter a valid phone number, e.g. 08031234567.")
            if User.objects.filter(phone=phone).exists():
                raise forms.ValidationError("An account with this phone number already exists.")
            self.cleaned_data["phone"] = phone
        return value

    def clean_date_of_birth(self):
        dob = self.cleaned_data["date_of_birth"]
        validate_age(dob)
        return dob

    def clean(self):
        data = super().clean()
        if data.get("password") and data.get("password") != data.get("confirm_password"):
            self.add_error("confirm_password", "Passwords do not match.")
        elif data.get("password"):
            probe = User(email=data.get("email"), first_name=data.get("first_name", ""),
                         last_name=data.get("last_name", ""))
            try:
                validate_password(data["password"], probe)
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return data


class LoginForm(forms.Form):
    identifier = forms.CharField(
        label="Phone number or email",
        widget=forms.TextInput(attrs={"autocomplete": "username", "autofocus": True}),
    )
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))


class OtpForm(forms.Form):
    code = forms.CharField(
        max_length=6,
        min_length=6,
        widget=forms.TextInput(
            attrs={"inputmode": "numeric", "autocomplete": "one-time-code", "pattern": r"\d{6}", "autofocus": True,
                   "class": "otp-input"}
        ),
    )


class CompleteProfileForm(forms.Form):
    """Used after Google sign-in, which doesn't give us date of birth."""

    first_name = forms.CharField(max_length=80)
    last_name = forms.CharField(max_length=80)
    date_of_birth = forms.DateField(widget=DateInput)
    accept_terms = forms.BooleanField(
        label="I am 18 or older and accept the Terms and Responsible Gaming policy.",
    )

    def clean_date_of_birth(self):
        dob = self.cleaned_data["date_of_birth"]
        validate_age(dob)
        return dob


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "date_of_birth"]
        widgets = {"date_of_birth": DateInput}

    def clean_date_of_birth(self):
        dob = self.cleaned_data["date_of_birth"]
        if dob:
            validate_age(dob)
        return dob


class KycForm(forms.Form):
    MAX_UPLOAD = 5 * 1024 * 1024
    ALLOWED_TYPES = ("image/jpeg", "image/png", "application/pdf")

    id_type = forms.ChoiceField(choices=KycSubmission.IdType.choices, label="ID type")
    id_number = forms.CharField(max_length=40, label="ID number")
    bank_code = forms.ChoiceField(label="Your bank")
    account_number = forms.CharField(
        max_length=10, min_length=10, label="Your account number",
        help_text="Must be an account in the same name as your ID.",
        widget=forms.TextInput(attrs={"inputmode": "numeric", "pattern": r"\d{10}", "placeholder": "10-digit NUBAN"}),
    )
    document = forms.FileField(
        required=False,
        label="Photo/scan of the ID (JPG, PNG or PDF, max 5 MB)",
        help_text="Required for passport, driver's licence and voter's card.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.payments.providers import payout_provider

        banks = sorted(payout_provider().list_banks(), key=lambda b: b[1])
        self.fields["bank_code"].choices = [("", "Select bank")] + banks
        # Order: ID first, then bank, then optional document.
        self.order_fields(["id_type", "id_number", "bank_code", "account_number", "document"])

    def clean_account_number(self):
        value = self.cleaned_data["account_number"].strip()
        if not value.isdigit():
            raise forms.ValidationError("Account number must be 10 digits.")
        return value

    def clean_document(self):
        doc = self.cleaned_data.get("document")
        if doc:
            if doc.size > self.MAX_UPLOAD:
                raise forms.ValidationError("File is larger than 5 MB.")
            if getattr(doc, "content_type", None) not in self.ALLOWED_TYPES:
                raise forms.ValidationError("Upload a JPG, PNG or PDF.")
        return doc

    def clean(self):
        data = super().clean()
        if data.get("id_type") not in ("nin", "bvn") and not data.get("document"):
            self.add_error("document", "Please upload a photo of this document.")
        return data
