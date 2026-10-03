"""Before coins existed, purchases were credited to the cash (now "winnings") balance.

Move each player's unspent purchased money from winnings into their coin balance, so only real prize
money stays withdrawable. Unspent purchases = cash balance minus net winnings (won - withdrawn), clipped
to [0, cash balance]. Recorded as a balanced journal transaction like any other money movement.
"""

import uuid

from django.db import migrations


def forwards(apps, schema_editor):
    Wallet = apps.get_model("ledger", "Wallet")
    LedgerAccount = apps.get_model("ledger", "LedgerAccount")
    JournalTransaction = apps.get_model("ledger", "JournalTransaction")
    Entry = apps.get_model("ledger", "Entry")

    for wallet in Wallet.objects.select_related("account", "user"):
        cash = wallet.account
        net_winnings = max(0, wallet.total_won - wallet.total_withdrawn)
        amount = max(0, min(cash.balance, cash.balance - net_winnings))
        if amount <= 0:
            continue
        coins = wallet.coin_account
        if coins is None:
            coins, _ = LedgerAccount.objects.get_or_create(
                code=f"player-coins:{wallet.user.public_id}:{wallet.currency}",
                defaults={"name": f"Player coins · {wallet.user_id} · {wallet.currency}", "kind": "liability",
                          "purpose": "player_coins", "currency": wallet.currency, "user": wallet.user},
            )
            wallet.coin_account = coins
            wallet.save(update_fields=["coin_account"])
        txn = JournalTransaction.objects.create(
            reference=uuid.uuid4(), idempotency_key=f"coin-conversion:{wallet.pk}", tx_type="deposit",
            currency=wallet.currency, description="Earlier purchase moved to coins",
            source_type="Wallet", source_id=str(wallet.pk), metadata={"migration": "0005"},
        )
        cash.balance -= amount
        cash.save(update_fields=["balance"])
        Entry.objects.create(transaction=txn, account=cash, direction="D", amount=amount, balance_after=cash.balance)
        coins.balance += amount
        coins.save(update_fields=["balance"])
        Entry.objects.create(transaction=txn, account=coins, direction="C", amount=amount, balance_after=coins.balance)


class Migration(migrations.Migration):
    dependencies = [("ledger", "0004_remove_ledgeraccount_player_cash_non_negative_and_more")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
