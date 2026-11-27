"""Harness for the Codex CLI in `codex exec --json` mode.

The event shapes follow the Codex documentation and the `exec_events.rs` types
in the Codex repository; they have not been checked against a live CLI here.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

from fledge.core import Turn, TurnResult

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _Envelope(BaseModel):
    type: str


class _ThreadStarted(BaseModel):
    thread_id: str


class _ErrorBody(BaseModel):
    message: str


class _TurnFailed(BaseModel):
    error: _ErrorBody


class _AgentMessage(BaseModel):
    type: Literal["agent_message"]
    text: str


class _CommandExecution(BaseModel):
    type: Literal["command_execution"]
    command: str
    exit_code: int | None = None


class _FileUpdate(BaseModel):
    path: str
    kind: str


class _FileChange(BaseModel):
    type: Literal["file_change"]
    changes: list[_FileUpdate]


class _McpToolCall(BaseModel):
    type: Literal["mcp_tool_call"]
    server: str
    tool: str


class _WebSearch(BaseModel):
    type: Literal["web_search"]
    query: str


class _ErrorItem(BaseModel):
    type: Literal["error"]
    message: str


class _OtherItem(BaseModel):
    type: str


_Item = Annotated[
    _AgentMessage
    | _CommandExecution
    | _FileChange
    | _McpToolCall
    | _WebSearch
    | _ErrorItem
    | _OtherItem,
    Field(union_mode="left_to_right"),
]


class _ItemEvent(BaseModel):
    item: _Item


@dataclass
class _Turn:
    thread_id: str | None = None
    final_message: str | None = None
    completed: bool = False
    failure: str | None = None
    last_error: str | None = None


def _describe_item(item: _Item) -> str | None:
    match item:
        case _AgentMessage():
            description = item.text
        case _CommandExecution():
            description = f"$ {item.command} (exit {item.exit_code})"
        case _FileChange():
            description = "; ".join(f"{c.kind} {c.path}" for c in item.changes)
        case _McpToolCall():
            description = f"{item.server}.{item.tool}"
        case _WebSearch():
            description = f"search: {item.query}"
        case _ErrorItem():
            description = f"error: {item.message}"
        case _:
            return None
    return description


def _handle_line(line: str, turn: _Turn, on_event: Callable[[str], None]) -> None:
    kind = _Envelope.model_validate_json(line).type
    if kind == "thread.started":
        turn.thread_id = _ThreadStarted.model_validate_json(line).thread_id
    elif kind == "item.completed":
        item = _ItemEvent.model_validate_json(line).item
        if isinstance(item, _AgentMessage):
            turn.final_message = item.text
        description = _describe_item(item)
        if description:
            on_event(description)
    elif kind == "turn.completed":
        turn.completed = True
    elif kind == "turn.failed":
        turn.failure = _TurnFailed.model_validate_json(line).error.message
    elif kind == "error":
        # Codex also emits this for recoverable problems such as reconnect
        # attempts, so it is reported rather than treated as fatal.
        turn.last_error = _ErrorBody.model_validate_json(line).message
        on_event(f"error: {turn.last_error}")


def _strict_schema(schema: JsonValue) -> JsonValue:
    """Copy of a JSON Schema that OpenAI structured outputs accept.

    Strict mode needs every object to forbid extra properties and to list all
    of its properties as required; pydantic's schemas do neither. Optional
    fields still work because pydantic gives them a schema that allows null.
    """
    if isinstance(schema, list):
        return [_strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    strict = {key: _strict_schema(value) for key, value in schema.items()}
    properties = strict.get("properties")
    if strict.get("type") == "object" and isinstance(properties, dict):
        strict["additionalProperties"] = False
        strict["required"] = list(properties)
    return strict


def _turn_result(
    turn: Turn, progress: _Turn, returncode: int, stderr_text: str
) -> TurnResult:
    if progress.failure is not None:
        raise RuntimeError(f"codex turn failed: {progress.failure}")
    if returncode != 0:
        raise RuntimeError(f"codex exited with status {returncode}: {stderr_text}")
    if not progress.completed:
        raise RuntimeError(f"codex never completed the turn: {progress.last_error}")
    if progress.final_message is None:
        raise RuntimeError("codex finished without a final message")
    # A resumed run may not repeat thread.started; the thread is unchanged.
    thread_id = progress.thread_id or turn.session_id
    if thread_id is None:
        raise RuntimeError("codex finished without a thread id")
    if turn.output_schema is None:
        return TurnResult(session_id=thread_id, final_message=progress.final_message)
    # With --output-schema the final agent message is the JSON answer.
    try:
        structured = _JSON.validate_json(progress.final_message)
    except ValidationError as err:
        raise RuntimeError(
            f"codex final message is not JSON: {progress.final_message!r}"
        ) from err
    return TurnResult(
        session_id=thread_id,
        final_message=progress.final_message,
        structured=structured,
    )


@dataclass(frozen=True)
class Codex:
    model: str | None = None
    executable: str = "codex"

    def run_turn(self, turn: Turn, on_event: Callable[[str], None]) -> TurnResult:
        # Options go before `resume` because a resumed run otherwise keeps the
        # working directory it was started with. The trailing "-" makes Codex
        # read the prompt from stdin, so a prompt starting with "-" is safe.
        argv = [
            self.executable,
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C",
            str(turn.workdir),
        ]
        if self.model is not None:
            argv += ["--model", self.model]
        # Codex keeps its sessions under CODEX_HOME, so pointing it at
        # state_dir lets a turn in a later container resume them.
        env = {**os.environ, "CODEX_HOME": str(turn.state_dir)}

        progress = _Turn()
        # The schema file goes outside the workdir so it never lands in a
        # commit. stderr goes to a file so a chatty stderr cannot fill its pipe
        # and stall the process while we block on stdout.
        with (
            tempfile.TemporaryDirectory() as scratch,
            tempfile.TemporaryFile() as stdin,
            tempfile.TemporaryFile() as stderr,
        ):
            if turn.output_schema is not None:
                schema_file = Path(scratch) / "output_schema.json"
                schema = _strict_schema(dict(turn.output_schema))
                schema_file.write_text(json.dumps(schema))
                # The Codex source declares --output-schema as a global flag,
                # so it should parse before `resume` too. Unverified on the
                # pinned 0.172.0: codex is not installed where this was written.
                argv += ["--output-schema", str(schema_file)]
            if turn.session_id is not None:
                argv += ["resume", turn.session_id]
            argv.append("-")
            stdin.write(turn.prompt.encode())
            stdin.seek(0)
            with subprocess.Popen(  # noqa: S603 -- fixed argv without a shell; the step container is the sandbox
                argv,
                cwd=turn.workdir,
                env=env,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
            ) as proc:
                # Leaving the Popen block waits for the process, so a failure
                # while reading must kill it first or that wait could hang.
                try:
                    assert proc.stdout is not None  # noqa: S101 -- guaranteed by stdout=PIPE; narrows the type
                    for line in proc.stdout:
                        if line.strip():
                            _handle_line(line, progress, on_event)
                except BaseException:
                    proc.kill()
                    raise
            returncode = proc.returncode
            stderr.seek(0)
            stderr_text = stderr.read().decode(errors="replace").strip()

        return _turn_result(turn, progress, returncode, stderr_text)
