"""Live business rules: code defaults (settings.NEXTGEN) overridden by Admin → Platform settings."""

from collections.abc import Mapping

from django.conf import settings


def rules() -> dict:
    from .models import PlatformSettings

    data = dict(settings.NEXTGEN)
    saved = PlatformSettings.objects.filter(pk=1).first()
    if saved is not None:
        data.update(saved.as_rules())
    return data


class _Rules(Mapping):
    """`RULES["MIN_DEPOSIT"]` always returns the current value, so admin changes apply immediately."""

    def __getitem__(self, key):
        return rules()[key]

    def __iter__(self):
        return iter(rules())

    def __len__(self):
        return len(rules())


RULES = _Rules()
