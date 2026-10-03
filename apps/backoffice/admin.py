"""Keep the admin menu short: technical tables stay reachable by link but are hidden from the menu."""

from django.apps import apps
from django.contrib import admin

HIDDEN_FROM_MENU = [
    "ledger.LedgerAccount", "ledger.JournalTransaction", "ledger.Wallet",
    "payments.PayoutAccount", "payments.WebhookEvent",
    "notifications.Notification",
    "compliance.ResponsibleGamingSettings", "compliance.RiskFlag",
    "admin.LogEntry", "games.Ticket",
]

for label in HIDDEN_FROM_MENU:
    model = apps.get_model(label)
    model_admin = admin.site._registry.get(model)
    if model_admin is not None:
        model_admin.has_module_permission = lambda request: False
