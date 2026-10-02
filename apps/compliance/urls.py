from django.urls import path

from . import views

app_name = "compliance"

urlpatterns = [
    path("", views.settings_view, name="settings"),
    path("self-exclude/", views.self_exclude, name="self_exclude"),
]
