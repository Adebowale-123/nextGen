from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("provably-fair/", views.fairness, name="fairness"),
    path("healthz/", views.healthz, name="healthz"),
]
