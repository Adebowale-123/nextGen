"""In-app notifications plus SMS and email delivery."""

import logging

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.tasks import task

from .models import Notification

log = logging.getLogger("apps.notifications")


def send_sms(phone: str, message: str):
    if settings.SMS_BACKEND == "termii":
        response = requests.post(
            f"{settings.TERMII_BASE_URL}/api/sms/send",
            json={
                "api_key": settings.TERMII_API_KEY,
                "to": phone.lstrip("+"),
                "from": settings.TERMII_SENDER_ID,
                "sms": message,
                "type": "plain",
                "channel": "dnd",
            },
            timeout=15,
        )
        if response.status_code >= 400:
            log.error("Termii SMS failed (%s): %s", response.status_code, response.text[:300])
            raise RuntimeError("SMS delivery failed.")
        return
    log.info("[SMS to %s] %s", phone, message)
    print(f"\n[SMS -> {phone}] {message}\n")


def send_email(address: str, subject: str, message: str):
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [address])


@task
def deliver_external(user_id: int, title: str, body: str, sms: bool, email: bool):
    from apps.accounts.models import User

    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return
    try:
        if sms and user.phone:
            send_sms(user.phone, f"{settings.NEXTGEN['BRAND_NAME']}: {body}")
        if email and user.email:
            send_email(user.email, title, body)
    except Exception:  # delivery failures must never break money flows
        log.exception("External notification delivery failed for user %s", user_id)


def notify(user, title, body, *, kind=Notification.Kind.INFO, link="", sms=False, email=False):
    """Create an in-app notification; SMS/email go out after the DB commit."""
    note = Notification.objects.create(user=user, kind=kind, title=title, body=body, link=link)
    if sms or email:
        transaction.on_commit(lambda: deliver_external.enqueue(user.pk, title, body, sms, email))
    return note
