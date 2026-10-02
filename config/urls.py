from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "NextGen Game Back Office"
admin.site.site_title = "NextGen Game Admin"
admin.site.index_title = "Operations"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("backoffice/", include("apps.backoffice.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("wallet/", include("apps.payments.urls")),
    path("responsible-gaming/", include("apps.compliance.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("", include("apps.games.urls")),
    path("", include("apps.core.urls")),
]
