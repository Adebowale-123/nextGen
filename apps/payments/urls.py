from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("", views.wallet, name="wallet"),
    path("deposit/", views.deposit, name="deposit"),
    path("deposit/<str:reference>/return/", views.deposit_return, name="deposit_return"),
    path("deposit/<str:reference>/sandbox-checkout/", views.mock_checkout, name="mock_checkout"),
    path("withdraw/", views.withdraw, name="withdraw"),
    path("withdraw/accounts/add/", views.add_payout_account, name="add_payout_account"),
    path("withdraw/<str:reference>/cancel/", views.cancel_withdrawal, name="cancel_withdrawal"),
    path("webhooks/<str:provider>/", views.webhook, name="webhook"),
]
