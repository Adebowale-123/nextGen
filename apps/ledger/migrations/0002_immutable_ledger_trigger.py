"""Make journal transactions and entries append-only at the database level (PostgreSQL)."""

from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION ledger_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Ledger records are immutable (% on %)', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_entry_immutable
    BEFORE UPDATE OR DELETE ON ledger_entry
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation();

CREATE TRIGGER ledger_journaltransaction_immutable
    BEFORE UPDATE OR DELETE ON ledger_journaltransaction
    FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation();
"""

DROP = """
DROP TRIGGER IF EXISTS ledger_entry_immutable ON ledger_entry;
DROP TRIGGER IF EXISTS ledger_journaltransaction_immutable ON ledger_journaltransaction;
DROP FUNCTION IF EXISTS ledger_reject_mutation();
"""


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        # params=None: send the SQL as-is, so psycopg doesn't read the '%' in RAISE as placeholders.
        schema_editor.execute(CREATE, params=None)


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP, params=None)


class Migration(migrations.Migration):
    dependencies = [("ledger", "0001_initial")]

    operations = [migrations.RunPython(forwards, backwards)]
