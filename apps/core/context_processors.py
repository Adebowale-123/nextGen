from django.conf import settings

from apps.ledger.services import get_wallet


def platform(request):
    context = {"BRAND_NAME": settings.NEXTGEN["BRAND_NAME"], "DEMO_MODE": settings.DEMO_MODE}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        context["unread_notifications"] = user.notifications.filter(read_at__isnull=True).count()
        context["nav_wallet"] = get_wallet(user, settings.NEXTGEN["DEFAULT_CURRENCY"])
    return context
