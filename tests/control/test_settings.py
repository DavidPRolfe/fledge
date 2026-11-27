from pathlib import Path

import pytest

from fledge.control.settings import Settings


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEDGE_STEP_SECRETS", "GITHUB_TOKEN, CLAUDE_CODE_OAUTH_TOKEN")
    monkeypatch.setenv("FLEDGE_WORKFLOWS_DIR", "/etc/fledge/workflows")

    settings = Settings()

    assert settings.max_concurrent_steps == 1
    assert settings.step_secrets == ["GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]
    assert settings.workflows_dir == Path("/etc/fledge/workflows")
