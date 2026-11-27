"""Harness for the Claude Code CLI in headless stream-json mode."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue

from fledge.harnesses.base import Turn, TurnResult, run_cli

_SUMMARY_KEYS = ("command", "file_path", "pattern", "url", "description")

# The tool Claude Code adds for --json-schema. Calling it is how the agent
# hands back its answer, not a step it chose, so it is not reported.
_STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


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
    # Absent unless --json-schema was given and the agent produced a valid
    # answer; model_fields_set tells absent apart from a JSON null answer.
    structured_output: JsonValue = None


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
            elif (
                isinstance(block, _ToolUseBlock)
                and block.name != _STRUCTURED_OUTPUT_TOOL
            ):
                on_event(_describe_tool_use(block))
    elif kind == "result":
        return _ResultEvent.model_validate_json(line)
    return None


def _turn_result(
    turn: Turn, result: _ResultEvent | None, returncode: int, stderr_text: str
) -> TurnResult:
    # An error result can carry subtype "success" (seen live on an auth
    # failure), so is_error is trusted over the subtype. The SDK types do
    # not promise is_error on the error subtypes (error_max_turns,
    # error_max_structured_output_retries, ...), so those count as well.
    if result is not None and (result.is_error or result.subtype.startswith("error_")):
        raise RuntimeError(
            f"claude reported an error ({result.subtype}): {result.result}"
        )
    if returncode != 0:
        raise RuntimeError(f"claude exited with status {returncode}: {stderr_text}")
    if result is None or result.result is None:
        raise RuntimeError("claude finished without a final message")
    if not result.session_id:
        raise RuntimeError("claude finished without a session id")
    if turn.output_schema is None:
        return TurnResult(session_id=result.session_id, final_message=result.result)
    if "structured_output" not in result.model_fields_set:
        raise RuntimeError("claude finished without a structured output")
    return TurnResult(
        session_id=result.session_id,
        final_message=result.result,
        structured=result.structured_output,
    )


@dataclass(frozen=True)
class ClaudeCode:
    model: str | None = None
    executable: str = "claude"

    def run_turn(self, turn: Turn, on_event: Callable[[str], None]) -> TurnResult:
        argv = [
            self.executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if self.model is not None:
            argv += ["--model", self.model]
        if turn.output_schema is not None:
            argv += ["--json-schema", json.dumps(turn.output_schema)]
        if turn.session_id is not None:
            argv += ["--resume", turn.session_id]
        result: _ResultEvent | None = None

        def on_line(line: str) -> None:
            nonlocal result
            result = _handle_line(line, on_event) or result

        # Claude Code keeps its sessions under the config dir, so pointing it
        # at state_dir lets a turn in a later container resume them.
        returncode, stderr_text = run_cli(
            argv, turn, {"CLAUDE_CONFIG_DIR": str(turn.state_dir)}, on_line
        )
        return _turn_result(turn, result, returncode, stderr_text)
