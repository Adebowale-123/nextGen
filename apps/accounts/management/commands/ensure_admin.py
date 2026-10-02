import os

from django.core.management.base import BaseCommand

from apps.accounts.models import User


class Command(BaseCommand):
    help = "Create the first superuser from ADMIN_EMAIL / ADMIN_PASSWORD if it doesn't exist (used on deploy)."

    def handle(self, *args, **options):
        email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not email or not password:
            self.stdout.write("ADMIN_EMAIL / ADMIN_PASSWORD not set; skipping admin creation.")
            return
        if User.objects.filter(email__iexact=email).exists():
            self.stdout.write(f"Admin {email} already exists; leaving it unchanged.")
            return
        User.objects.create_superuser(email, password, first_name="Admin")
        self.stdout.write(self.style.SUCCESS(f"Created admin {email}."))
