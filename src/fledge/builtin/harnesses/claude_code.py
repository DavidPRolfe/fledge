"""Harness for the Claude Code CLI in headless stream-json mode."""

import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue

from fledge.core import TurnResult

_SUMMARY_KEYS = ("command", "file_path", "pattern", "url", "description")


class _Envelope(BaseModel):
    type: str


class _TextBlock(BaseModel):
    type: Literal["text"]
    text: str


class _ToolUseBlock(BaseModel):
    type: Literal["tool_use"]
    name: str
    input: dict[str, JsonValue]


class _OtherBlock(BaseModel):
    type: str


class _Message(BaseModel):
    content: list[
        Annotated[
            _TextBlock | _ToolUseBlock | _OtherBlock,
            Field(union_mode="left_to_right"),
        ]
    ]


class _AssistantEvent(BaseModel):
    message: _Message


class _ResultEvent(BaseModel):
    subtype: str
    is_error: bool
    session_id: str
    # Absent on some error subtypes such as error_max_turns.
    result: str | None = None


def _describe_tool_use(block: _ToolUseBlock) -> str:
    for key in _SUMMARY_KEYS:
        value = block.input.get(key)
        if isinstance(value, str):
            return f"{block.name}: {value}"
    return block.name


def _handle_line(line: str, on_event: Callable[[str], None]) -> _ResultEvent | None:
    kind = _Envelope.model_validate_json(line).type
    if kind == "assistant":
        for block in _AssistantEvent.model_validate_json(line).message.content:
            if isinstance(block, _TextBlock):
                on_event(block.text)
            elif isinstance(block, _ToolUseBlock):
                on_event(_describe_tool_use(block))
    elif kind == "result":
        return _ResultEvent.model_validate_json(line)
    return None


class ClaudeCodeHarness:
    def __init__(self, executable: str = "claude") -> None:
        self._executable = executable

    def run_turn(
        self,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        on_event: Callable[[str], None],
    ) -> TurnResult:
        argv = [
            self._executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if session_id is not None:
            argv += ["--resume", session_id]

        result: _ResultEvent | None = None
        # The prompt goes in on stdin so one starting with "-" cannot be read
        # as an option. stderr goes to a file so a chatty stderr cannot fill
        # its pipe and stall the process while we block on stdout.
        with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stderr:
            stdin.write(prompt.encode())
            stdin.seek(0)
            with subprocess.Popen(  # noqa: S603 -- fixed argv without a shell; the step container is the sandbox
                argv,
                cwd=workspace,
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
                            result = _handle_line(line, on_event) or result
                except BaseException:
                    proc.kill()
                    raise
            returncode = proc.returncode
            stderr.seek(0)
            stderr_text = stderr.read().decode(errors="replace").strip()

        # An error result can carry subtype "success" (seen live on an auth
        # failure), so is_error is the field to trust.
        if result is not None and result.is_error:
            raise RuntimeError(
                f"claude reported an error ({result.subtype}): {result.result}"
            )
        if returncode != 0:
            raise RuntimeError(f"claude exited with status {returncode}: {stderr_text}")
        if result is None or result.result is None:
            raise RuntimeError("claude finished without a final message")
        if not result.session_id:
            raise RuntimeError("claude finished without a session id")
        return TurnResult(session_id=result.session_id, final_message=result.result)
