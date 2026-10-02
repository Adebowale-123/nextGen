from django.contrib.auth.backends import ModelBackend

from .models import User, normalize_phone


class IdentifierBackend(ModelBackend):
    """Authenticate with either an email address or a phone number.

    Used by both the player login and the admin login, so the brute-force lockout protects both.
    """

    def authenticate(self, request, username=None, password=None, identifier=None, **kwargs):
        from .services import clear_login_failures, is_locked_out, record_login_failure

        identifier = identifier or username or kwargs.get("email")
        if not identifier or not password or is_locked_out(identifier):
            return None
        user = find_user_by_identifier(identifier)
        if user is None:
            User().set_password(password)  # equalise timing with the found-user path
            record_login_failure(identifier)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            clear_login_failures(identifier)
            return user
        record_login_failure(identifier)
        return None


def find_user_by_identifier(identifier: str):
    identifier = identifier.strip()
    if "@" in identifier:
        return User.objects.filter(email__iexact=identifier).first()
    return User.objects.filter(phone=normalize_phone(identifier)).first()
