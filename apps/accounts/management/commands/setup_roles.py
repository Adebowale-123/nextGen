from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

VIEW_ALL = [("accounts", "user"), ("accounts", "kycsubmission"), ("payments", "deposit"), ("payments", "withdrawal"),
            ("payments", "payoutaccount"), ("payments", "webhookevent"), ("games", "game"), ("games", "draw"),
            ("games", "ticket"), ("ledger", "journaltransaction"), ("ledger", "ledgeraccount"), ("ledger", "wallet"),
            ("compliance", "riskflag"), ("compliance", "responsiblegamingsettings"), ("notifications", "notification"),
            ("core", "platformsettings")]

ROLES = {
    # Can look at everything, change nothing.
    "Viewer": {"view": VIEW_ALL, "change": []},
    # Day-to-day operations: review KYC and withdrawals, spin games, manage player status and flags.
    "Operations": {
        "view": VIEW_ALL,
        "change": [("accounts", "user"), ("accounts", "kycsubmission"), ("payments", "withdrawal"),
                   ("games", "draw"), ("compliance", "riskflag")],
    },
    # Business settings: prices, prizes, game times, platform rules.
    "Game manager": {
        "view": VIEW_ALL,
        "change": [("games", "game"), ("games", "draw"), ("core", "platformsettings")],
    },
}


class Command(BaseCommand):
    help = "Create staff roles (Viewer, Operations, Game manager). Safe to run again."

    def handle(self, *args, **options):
        for name, spec in ROLES.items():
            group, _ = Group.objects.get_or_create(name=name)
            perms = []
            for action in ("view", "change"):
                for app, model in spec[action]:
                    perm = Permission.objects.filter(content_type__app_label=app, codename=f"{action}_{model}").first()
                    if perm:
                        perms.append(perm)
            group.permissions.set(perms)
            self.stdout.write(f"Role '{name}': {len(perms)} permissions")
        self.stdout.write(self.style.SUCCESS(
            "Done. Give a staff member a role in Admin -> Users -> (user) -> Staff access -> Groups, "
            "and tick 'Staff status'."))
