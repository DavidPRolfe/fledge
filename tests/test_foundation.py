from pathlib import Path

import psycopg
import pytest

from fledge.config import load_config
from fledge.core import Workflow
from fledge.db import migrate


class _Step:
    def __init__(self, name: str) -> None:
        self.name = name

    def run(self, ctx: object) -> dict[str, str]:
        return {}


def test_workflow_rejects_duplicate_step_names() -> None:
    with pytest.raises(ValueError, match="duplicate step names"):
        Workflow(name="w", steps=[_Step("a"), _Step("a")])


def test_trigger_config_collects_extra_keys_as_options(tmp_path: Path) -> None:
    path = tmp_path / "fledge.toml"
    path.write_text(
        """
image = "fledge:dev"
api_url = "http://fledge:8000"
docker_network = "fledge"

[[trigger]]
name = "api-issues"
type = "github_issues"
workflow = "ticket_to_pr"
repo = "acme/api"
label = "fledge"
"""
    )

    config = load_config(path)

    assert config.max_concurrent_runs == 1
    assert config.trigger[0].options == {"repo": "acme/api", "label": "fledge"}


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
