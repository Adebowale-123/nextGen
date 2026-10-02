from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register, name="register"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("verify/", views.verify, name="verify"),
    path("verify/resend/", views.resend_otp, name="resend_otp"),
    path("profile/", views.profile, name="profile"),
    path("kyc/", views.kyc, name="kyc"),
    path("oauth/google/", views.google_start, name="google_start"),
    path("oauth/google/callback/", views.google_callback, name="google_callback"),
    path("oauth/google/complete/", views.google_complete, name="google_complete"),
    path("kyc/documents/<int:pk>/", views.kyc_document, name="kyc_document"),
]
