import psycopg

from fledge.control.db import migrate


def test_migrate_creates_schema_and_is_repeatable(database_url: str) -> None:
    migrate(database_url)

    with psycopg.connect(database_url) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        versions = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()

    assert {"runs", "step_runs", "events", "guidance", "workflow_snapshots"} <= tables
    assert versions == (3,)
