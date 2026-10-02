"""Third-party identity verification adapters.

Each provider returns a KycResult. A "match" means the ID exists and the name on
record matches the player's profile; anything else goes to manual review.
"""

import re
from dataclasses import dataclass, field

import requests
from django.conf import settings


@dataclass
class KycResult:
    """status: "verified" (ID exists), "failed" (ID invalid / not found) or "review" (needs a human)."""

    status: str
    note: str
    first_name: str = ""
    last_name: str = ""
    reference: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def verified(self):
        return self.status == "verified"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


ID_FORMATS = {
    "nin": r"^\d{11}$",
    "bvn": r"^\d{11}$",
    "passport": r"^[A-Z]\d{8}$",
    "drivers_license": r"^[A-Z0-9]{8,20}$",
    "voters_card": r"^[A-Z0-9]{9,20}$",
}


def valid_id_format(id_type, id_number):
    pattern = ID_FORMATS.get(id_type)
    return bool(pattern and re.match(pattern, id_number.upper()))


def name_tokens(name):
    return {t for t in re.sub(r"[^A-Z ]", " ", (name or "").upper()).split() if len(t) > 1}


def id_name_matches_bank(first_name, last_name, bank_account_name):
    """Every part of the first and last name on the ID must appear in the bank account name (any order)."""
    needed = name_tokens(first_name) | name_tokens(last_name)
    return bool(needed) and needed <= name_tokens(bank_account_name)


class MockKycProvider:
    """Sandbox provider: valid-format IDs verify (in the player's profile name); IDs ending in 0000 are not found."""

    code = "mock"

    def verify(self, user, id_type, id_number) -> KycResult:
        if not valid_id_format(id_type, id_number):
            return KycResult("failed", "ID number format is invalid.")
        if id_number.endswith("0000"):
            return KycResult("failed", "ID not found in the registry.", raw={"sandbox": True})
        return KycResult("verified", "ID found (sandbox).", first_name=user.first_name, last_name=user.last_name,
                         reference=f"mock-{id_number[-4:]}", raw={"sandbox": True})


class DojahKycProvider:
    """Dojah (https://dojah.io) NIN / BVN lookup. Other ID types go to manual review."""

    code = "dojah"
    ENDPOINTS = {"nin": ("/api/v1/kyc/nin", "nin"), "bvn": ("/api/v1/kyc/bvn/full", "bvn")}

    def verify(self, user, id_type, id_number) -> KycResult:
        if id_type not in self.ENDPOINTS:
            return KycResult("review", "This document type is checked by our team.")
        path, param = self.ENDPOINTS[id_type]
        try:
            response = requests.get(
                settings.DOJAH_BASE_URL + path,
                params={param: id_number},
                headers={"AppId": settings.DOJAH_APP_ID, "Authorization": settings.DOJAH_SECRET_KEY},
                timeout=20,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            return KycResult("review", f"Verification service unavailable ({exc.__class__.__name__}).")
        entity = data.get("entity") or {}
        if response.status_code >= 500:
            return KycResult("review", "Verification service error.", raw=_redact(data))
        if response.status_code != 200 or not entity:
            return KycResult("failed", data.get("error") or "ID not found.", raw=_redact(data))
        return KycResult("verified", "ID found via Dojah.", first_name=entity.get("first_name", ""),
                         last_name=entity.get("last_name", ""), reference=id_number[-4:], raw=_redact(data))


def _redact(data):
    """Keep provider payloads auditable without storing photos or full PII."""
    entity = dict(data.get("entity") or {})
    for key in ("photo", "image", "signature", "phone_number", "email", "residential_address"):
        entity.pop(key, None)
    return {"entity": entity, "error": data.get("error")}


def get_kyc_provider():
    return {"mock": MockKycProvider, "dojah": DojahKycProvider}[settings.KYC_PROVIDER]()
