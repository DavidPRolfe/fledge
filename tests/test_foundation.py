from pathlib import Path

import psycopg
import pytest

from fledge.db import migrate
from fledge.settings import Settings


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEDGE_STEP_SECRETS", "GITHUB_TOKEN, CLAUDE_CODE_OAUTH_TOKEN")
    monkeypatch.setenv("FLEDGE_WORKFLOWS_DIR", "/etc/fledge/workflows")

    settings = Settings()

    assert settings.max_concurrent_steps == 1
    assert settings.step_secrets == ["GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]
    assert settings.workflows_dir == Path("/etc/fledge/workflows")


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
