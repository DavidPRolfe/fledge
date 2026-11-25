import psycopg
import pytest

from fledge.core import Workflow
from fledge.db import migrate
from fledge.settings import Settings


class _Step:
    def __init__(self, name: str) -> None:
        self.name = name

    def run(self, ctx: object) -> dict[str, str]:
        return {}


def test_workflow_rejects_duplicate_step_names() -> None:
    with pytest.raises(ValueError, match="duplicate step names"):
        Workflow(name="w", steps=[_Step("a"), _Step("a")])


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEDGE_STEP_SECRETS", "GITHUB_TOKEN, CLAUDE_CODE_OAUTH_TOKEN")
    monkeypatch.setenv(
        "FLEDGE_TRIGGERS",
        '[{"name": "api-issues", "type": "github_issues", "workflow": "ticket_to_pr",'
        ' "repo": "acme/api", "label": "fledge"}]',
    )

    settings = Settings()

    assert settings.max_concurrent_runs == 1
    assert settings.step_secrets == ["GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]
    assert settings.triggers[0].options == {"repo": "acme/api", "label": "fledge"}


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

    assert {"runs", "step_runs", "events", "guidance"} <= tables
    assert versions == (1,)
