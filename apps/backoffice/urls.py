from django.urls import path

from . import views

app_name = "backoffice"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("reports/", views.financial_report, name="report"),
    path("ledger/", views.ledger_overview, name="ledger"),
    path("scheduler/tick/", views.scheduler_tick, name="scheduler_tick"),
    path("spin/", views.spin_queue, name="spin_queue"),
    path("spin/<int:pk>/", views.spin, name="spin"),
]
