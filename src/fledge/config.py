import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

_TRIGGER_KEYS = {"name", "type", "workflow", "options"}


class TriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    workflow: str
    options: dict[str, object] = {}

    @model_validator(mode="before")
    @classmethod
    def _collect_options(cls, data: object) -> object:
        if not isinstance(data, dict) or "options" in data:
            return data
        fixed = {k: v for k, v in data.items() if k in _TRIGGER_KEYS}
        options = {k: v for k, v in data.items() if k not in _TRIGGER_KEYS}
        return {**fixed, "options": options}


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    api_url: str
    """Where step containers reach the control plane, e.g. http://fledge:8000."""
    docker_network: str
    max_concurrent_runs: int = 1
    poll_interval_seconds: int = 30
    step_timeout_minutes: int = 60
    step_secrets: list[str] = []
    step_source_dir: Path | None = None
    """Host directory bind-mounted over the image's src/ in step containers, so
    a local checkout's step code runs without rebuilding the image."""
    trigger: list[TriggerConfig] = []


def load_config(path: Path) -> Config:
    with path.open("rb") as file:
        return Config.model_validate(tomllib.load(file))
