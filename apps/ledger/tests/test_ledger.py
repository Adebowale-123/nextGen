from django.test import TestCase

from apps.core.tests.helpers import balance, make_user
from apps.ledger.models import Entry, ImmutableRecordError, JournalTransaction, LedgerAccount
from apps.ledger.services import (
    InsufficientFunds,
    LedgerError,
    credit,
    debit,
    get_system_account,
    post_transaction,
    verify_ledger_integrity,
)

T = JournalTransaction.Type


class LedgerTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = self.user.wallets.get()
        self.clearing = get_system_account(LedgerAccount.Purpose.PROVIDER_CLEARING, "NGN", "mock")
        self.house = get_system_account(LedgerAccount.Purpose.HOUSE_REVENUE, "NGN")

    def test_wallet_initialised_at_zero(self):
        self.assertEqual(self.wallet.account.balance, 0)
        self.assertEqual(self.wallet.account.purpose, LedgerAccount.Purpose.PLAYER_CASH)

    def test_balanced_posting_updates_both_sides(self):
        post_transaction(T.DEPOSIT, [debit(self.clearing, 5000), credit(self.wallet.account, 5000)])
        self.clearing.refresh_from_db()
        self.assertEqual(balance(self.user), 5000)
        self.assertEqual(self.clearing.balance, 5000)  # asset: debit increases
        self.assertEqual(verify_ledger_integrity(), [])

    def test_unbalanced_rejected(self):
        with self.assertRaises(LedgerError):
            post_transaction(T.DEPOSIT, [debit(self.clearing, 5000), credit(self.wallet.account, 4000)])
        self.assertEqual(JournalTransaction.objects.count(), 0)

    def test_non_positive_and_float_amounts_rejected(self):
        for amount in (0, -5, 10.5):
            with self.assertRaises(LedgerError):
                post_transaction(T.DEPOSIT, [debit(self.clearing, amount), credit(self.wallet.account, amount)])

    def test_player_cannot_go_negative_and_nothing_is_written(self):
        with self.assertRaises(InsufficientFunds):
            post_transaction(T.TICKET_PURCHASE, [debit(self.wallet.account, 100), credit(self.house, 100)])
        self.assertEqual(balance(self.user), 0)
        self.assertEqual(Entry.objects.count(), 0)

    def test_idempotency_key_posts_once(self):
        legs = [debit(self.clearing, 700), credit(self.wallet.account, 700)]
        first = post_transaction(T.DEPOSIT, legs, idempotency_key="dep:1")
        second = post_transaction(T.DEPOSIT, legs, idempotency_key="dep:1")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(balance(self.user), 700)

    def test_entries_are_immutable(self):
        txn = post_transaction(T.DEPOSIT, [debit(self.clearing, 100), credit(self.wallet.account, 100)])
        entry = txn.entries.first()
        entry.amount = 1
        with self.assertRaises(ImmutableRecordError):
            entry.save()
        with self.assertRaises(ImmutableRecordError):
            entry.delete()
        with self.assertRaises(ImmutableRecordError):
            txn.delete()

    def test_mixed_currency_rejected(self):
        other = get_system_account(LedgerAccount.Purpose.HOUSE_REVENUE, "KES")
        with self.assertRaises(LedgerError):
            post_transaction(T.DEPOSIT, [debit(self.clearing, 100), credit(other, 100)])

    def test_integrity_check_detects_tampered_balance(self):
        post_transaction(T.DEPOSIT, [debit(self.clearing, 100), credit(self.wallet.account, 100)])
        LedgerAccount.objects.filter(pk=self.wallet.account.pk).update(balance=999)
        problems = verify_ledger_integrity()
        self.assertEqual(len(problems), 1)
        self.assertIn("stored balance 999", problems[0])
