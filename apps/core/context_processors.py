from django.conf import settings


def platform(request):
    context = {"BRAND_NAME": settings.NEXTGEN["BRAND_NAME"], "DEMO_MODE": settings.DEMO_MODE}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        context["unread_notifications"] = user.notifications.filter(read_at__isnull=True).count()
        wallet = user.wallets.filter(currency=settings.NEXTGEN["DEFAULT_CURRENCY"]).select_related("account").first()
        context["nav_wallet"] = wallet
    return context
