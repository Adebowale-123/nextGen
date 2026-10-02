"""Google OAuth 2.0 (authorization code flow) sign-in."""

import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.urls import reverse

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SESSION_STATE = "google_oauth_state"


class OAuthError(Exception):
    pass


def is_enabled():
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)


def redirect_uri():
    return settings.SITE_URL + reverse("accounts:google_callback")


def authorization_url(request):
    state = secrets.token_urlsafe(24)
    request.session[SESSION_STATE] = state
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(request, code, state):
    expected = request.session.pop(SESSION_STATE, None)
    if not expected or not secrets.compare_digest(expected, state or ""):
        raise OAuthError("Sign-in session expired. Please try again.")
    try:
        token = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=15,
        ).json()
        access_token = token["access_token"]
        info = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15).json()
    except (requests.RequestException, KeyError, ValueError) as exc:
        raise OAuthError("Google sign-in failed. Please try again.") from exc
    if not info.get("email") or not info.get("email_verified"):
        raise OAuthError("Your Google account email is not verified.")
    return {
        "email": info["email"].lower(),
        "first_name": info.get("given_name", ""),
        "last_name": info.get("family_name", ""),
    }
