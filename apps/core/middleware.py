import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout


class IdleSessionTimeoutMiddleware:
    """Log users out after SESSION_IDLE_TIMEOUT seconds without a request."""

    SESSION_KEY = "_last_activity"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            now = int(time.time())
            last = request.session.get(self.SESSION_KEY)
            if last and now - last > settings.SESSION_IDLE_TIMEOUT:
                logout(request)
                messages.info(request, "You were signed out after a period of inactivity.")
            else:
                request.session[self.SESSION_KEY] = now
        return self.get_response(request)
