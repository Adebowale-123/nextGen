import logging
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.core.cache import cache


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


class RequestDrivenSchedulerMiddleware:
    """Run the game scheduler on incoming requests (throttled) when no background worker exists.

    Enabled with SCHEDULER_ON_REQUEST=true (e.g. on Render's free plan). Ticket sales are already
    refused after a game's closing time, so a slightly late tick never lets anyone play late.
    """

    CACHE_KEY = "scheduler:last-request-tick"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.SCHEDULER_ON_REQUEST and not request.path.startswith("/" + settings.STATIC_URL.lstrip("/")):
            if cache.add(self.CACHE_KEY, True, settings.SCHEDULER_ON_REQUEST_SECONDS):
                try:
                    from apps.games.services import run_scheduler_tick

                    run_scheduler_tick()
                except Exception:  # never break a page because of the scheduler
                    logging.getLogger("apps.games").exception("Request-driven scheduler tick failed")
        return self.get_response(request)
