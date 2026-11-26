from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Control-plane settings, read from FLEDGE_* environment variables.

    The defaults suit the bundled compose.yaml.
    """

    model_config = SettingsConfigDict(env_prefix="FLEDGE_", extra="forbid")

    image: str = "fledge:dev"
    api_url: str = "http://fledge:8000"
    """Where step containers reach the control plane."""
    docker_network: str = "fledge"
    max_concurrent_runs: int = 1
    poll_interval_seconds: int = 30
    step_timeout_minutes: int = 60
    step_secrets: Annotated[list[str], NoDecode] = [
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ]
    """Environment variables copied into every step container when set,
    comma-separated."""
    step_source_dir: Path | None = None
    """Host directory bind-mounted over the image's src/ in step containers, so
    a local checkout's step code runs without rebuilding the image."""
    workflows_dir: Path = Path("workflows")
    """Directory of Python files defining the workflows, each with its trigger."""

    @field_validator("step_secrets", mode="before")
    @classmethod
    def _split_step_secrets(cls, value: object) -> object:
        if isinstance(value, str):
            return [name.strip() for name in value.split(",") if name.strip()]
        return value
