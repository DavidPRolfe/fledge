"""The interface a harness implements."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import JsonValue


@dataclass(frozen=True)
class Turn:
    prompt: str
    workdir: Path
    state_dir: Path
    """Where the harness keeps session state. It outlives the container, so a
    later turn in another container can resume the session."""
    session_id: str | None
    """None on an agent's first turn in a run."""
    output_schema: Mapping[str, JsonValue] | None = None
    """JSON Schema the agent's final answer must satisfy."""


@dataclass(frozen=True)
class TurnResult:
    session_id: str
    final_message: str
    structured: JsonValue = None
    """The final answer as JSON when the turn had an output schema."""


@runtime_checkable
class Harness(Protocol):
    def run_turn(self, turn: Turn, on_event: Callable[[str], None]) -> TurnResult:
        """Run the agent until it yields. Raise if the agent fails, or if
        `turn.session_id` cannot be resumed."""
        ...
