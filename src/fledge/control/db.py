import logging
from importlib.resources import files

import psycopg

logger = logging.getLogger(__name__)


def migrate(database_url: str) -> None:
    """Apply the SQL files in fledge/migrations that have not been applied yet."""
    migrations = sorted(
        (
            entry
            for entry in files("fledge.control.migrations").iterdir()
            if entry.name.endswith(".sql")
        ),
        key=lambda entry: entry.name,
    )
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        for migration in migrations:
            if migration.name in applied:
                continue
            logger.info("applying migration %s", migration.name)
            # Migration files are trusted package data and hold several
            # statements, which psycopg only accepts as a plain string.
            conn.execute(migration.read_text().encode())
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)",
                (migration.name,),
            )
