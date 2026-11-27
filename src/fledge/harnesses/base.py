"""The interface a harness implements."""

import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
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


def run_cli(
    argv: Sequence[str],
    turn: Turn,
    env: Mapping[str, str],
    on_line: Callable[[str], None],
) -> tuple[int, str]:
    """Run an agent CLI in the turn's workdir with the prompt on stdin.

    `env` is added to this process's environment. Calls `on_line` with each
    non-blank line of stdout, and returns the exit status and stderr.
    """
    # The prompt goes in on stdin so one starting with "-" cannot be read as an
    # option. stderr goes to a file so a chatty stderr cannot fill its pipe and
    # stall the process while we block on stdout.
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stderr:
        stdin.write(turn.prompt.encode())
        stdin.seek(0)
        with subprocess.Popen(  # noqa: S603 - argv built by a harness, no shell; the step container is the sandbox
            argv,
            cwd=turn.workdir,
            env={**os.environ, **env},
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
        ) as proc:
            # Leaving the Popen block waits for the process, so a failure
            # while reading must kill it first or that wait could hang.
            try:
                assert proc.stdout is not None  # noqa: S101 - guaranteed by stdout=PIPE; narrows the type
                for line in proc.stdout:
                    if line.strip():
                        on_line(line)
            except BaseException:
                proc.kill()
                raise
        stderr.seek(0)
        return proc.returncode, stderr.read().decode(errors="replace").strip()
