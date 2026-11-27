import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from fledge.harnesses import ClaudeCode, Codex
from fledge.harnesses.base import Harness, Turn

FAKE_CLI = """\
#!{python}
import json, os, sys
from pathlib import Path

here = Path(__file__).parent
argv = sys.argv[1:]
schema = None
if "--output-schema" in argv:
    schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
(here / "call.json").write_text(json.dumps({{
    "argv": argv,
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
    "env": {{
        name: os.environ.get(name) for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")
    }},
    "schema": schema,
}}))
if (here / "fail_resume").exists() and ("--resume" in argv or "resume" in argv):
    sys.stderr.write("No conversation found with that session ID\\n")
    sys.exit(1)
sys.stdout.write((here / "stdout.jsonl").read_text())
sys.stderr.write("fake stderr\\n")
sys.exit(int((here / "exit_code").read_text()))
"""


@dataclass
class FakeCli:
    path: Path

    def call(self) -> dict[str, JsonValue]:
        recorded: dict[str, JsonValue] = json.loads(
            (self.path.parent / "call.json").read_text()
        )
        return recorded

    def argv(self) -> list[JsonValue]:
        argv = self.call()["argv"]
        assert isinstance(argv, list)
        return argv


def fake_cli(
    tmp_path: Path,
    events: list[dict[str, JsonValue]],
    exit_code: int = 0,
    *,
    fail_resume: bool = False,
) -> FakeCli:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "agent"
    script.write_text(FAKE_CLI.format(python=sys.executable))
    script.chmod(0o755)
    (bin_dir / "stdout.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )
    (bin_dir / "exit_code").write_text(str(exit_code))
    if fail_resume:
        (bin_dir / "fail_resume").touch()
    return FakeCli(script)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def make_turn(
    workspace: Path,
    prompt: str = "go",
    session_id: str | None = None,
    schema: dict[str, JsonValue] | None = None,
) -> Turn:
    return Turn(prompt, workspace, workspace.parent / "state", session_id, schema)


CLAUDE_OK: list[dict[str, JsonValue]] = [
    {"type": "system", "subtype": "init", "session_id": "sess-1", "tools": []},
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "Looking at the code."},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "ls -la"},
                },
                {"type": "tool_use", "id": "t2", "name": "TodoWrite", "input": {}},
            ]
        },
    },
    {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
    },
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "sess-1",
        "result": "All done.",
    },
]

CODEX_OK: list[dict[str, JsonValue]] = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {
        "type": "item.started",
        "item": {
            "id": "i0",
            "type": "command_execution",
            "command": "ls",
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "i0",
            "type": "command_execution",
            "command": "ls",
            "aggregated_output": "a\n",
            "exit_code": 0,
            "status": "completed",
        },
    },
    {
        "type": "item.completed",
        "item": {"id": "i1", "type": "reasoning", "text": "hmm"},
    },
    {
        "type": "item.completed",
        "item": {
            "id": "i2",
            "type": "file_change",
            "changes": [{"path": "a.py", "kind": "update"}],
            "status": "completed",
        },
    },
    {
        "type": "item.completed",
        "item": {"id": "i3", "type": "agent_message", "text": "All done."},
    },
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 1,
            "cached_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_output_tokens": 0,
        },
    },
]

SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"approved": {"type": "boolean"}},
    "required": ["approved"],
}
ANSWER: JsonValue = {"approved": True}


def claude_events(**result: JsonValue) -> list[dict[str, JsonValue]]:
    return [*CLAUDE_OK[:-1], {**CLAUDE_OK[-1], **result}]


def codex_events(final_message: str) -> list[dict[str, JsonValue]]:
    return [
        *CODEX_OK[:-2],
        {
            "type": "item.completed",
            "item": {"id": "i3", "type": "agent_message", "text": final_message},
        },
        CODEX_OK[-1],
    ]


def test_harnesses_satisfy_protocol() -> None:
    assert isinstance(ClaudeCode(), Harness)
    assert isinstance(Codex(), Harness)


def test_claude_first_turn(tmp_path: Path, workspace: Path) -> None:
    cli = fake_cli(tmp_path, CLAUDE_OK)
    events: list[str] = []

    result = ClaudeCode(executable=str(cli.path)).run_turn(
        make_turn(workspace, "-fix the bug"), events.append
    )

    assert result.session_id == "sess-1"
    assert result.final_message == "All done."
    assert result.structured is None
    assert events == ["Looking at the code.", "Bash: ls -la", "TodoWrite"]
    call = cli.call()
    assert call["stdin"] == "-fix the bug"
    assert Path(str(call["cwd"])).resolve() == workspace.resolve()
    assert call["argv"] == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]


def test_claude_resumed_turn(tmp_path: Path, workspace: Path) -> None:
    cli = fake_cli(tmp_path, CLAUDE_OK)

    ClaudeCode(executable=str(cli.path)).run_turn(
        make_turn(workspace, "more", "sess-1"), lambda _: None
    )

    assert cli.argv()[-2:] == ["--resume", "sess-1"]


def test_codex_first_turn(tmp_path: Path, workspace: Path) -> None:
    cli = fake_cli(tmp_path, CODEX_OK)
    events: list[str] = []

    result = Codex(executable=str(cli.path)).run_turn(
        make_turn(workspace, "-fix the bug"), events.append
    )

    assert result.session_id == "thread-1"
    assert result.final_message == "All done."
    assert result.structured is None
    assert events == ["$ ls (exit 0)", "update a.py", "All done."]
    call = cli.call()
    assert call["stdin"] == "-fix the bug"
    assert Path(str(call["cwd"])).resolve() == workspace.resolve()
    assert call["argv"] == [
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C",
        str(workspace),
        "-",
    ]
    assert call["schema"] is None


def test_codex_resumed_turn(tmp_path: Path, workspace: Path) -> None:
    cli = fake_cli(tmp_path, CODEX_OK[1:])

    result = Codex(executable=str(cli.path)).run_turn(
        make_turn(workspace, "more", "thread-1"), lambda _: None
    )

    assert result.session_id == "thread-1"
    argv = cli.argv()
    assert argv[-3:] == ["resume", "thread-1", "-"]
    assert argv[argv.index("-C") + 1] == str(workspace)


@pytest.mark.parametrize(
    ("harness_type", "events", "variable"),
    [
        (ClaudeCode, CLAUDE_OK, "CLAUDE_CONFIG_DIR"),
        (Codex, CODEX_OK, "CODEX_HOME"),
    ],
)
def test_state_dir_reaches_the_cli(
    tmp_path: Path,
    workspace: Path,
    harness_type: type[ClaudeCode | Codex],
    events: list[dict[str, JsonValue]],
    variable: str,
) -> None:
    cli = fake_cli(tmp_path, events)

    harness_type(executable=str(cli.path)).run_turn(
        make_turn(workspace), lambda _: None
    )

    env = cli.call()["env"]
    assert isinstance(env, dict)
    assert env[variable] == str(tmp_path / "state")


@pytest.mark.parametrize(
    ("harness_type", "events"), [(ClaudeCode, CLAUDE_OK), (Codex, CODEX_OK)]
)
def test_model_flag(
    tmp_path: Path,
    workspace: Path,
    harness_type: type[ClaudeCode | Codex],
    events: list[dict[str, JsonValue]],
) -> None:
    cli = fake_cli(tmp_path, events)

    harness_type(model="some-model", executable=str(cli.path)).run_turn(
        make_turn(workspace), lambda _: None
    )

    argv = cli.argv()
    assert argv[argv.index("--model") + 1] == "some-model"


@pytest.mark.parametrize(
    ("harness_type", "events"), [(ClaudeCode, CLAUDE_OK), (Codex, CODEX_OK[1:])]
)
def test_unresumable_session_raises(
    tmp_path: Path,
    workspace: Path,
    harness_type: type[ClaudeCode | Codex],
    events: list[dict[str, JsonValue]],
) -> None:
    cli = fake_cli(tmp_path, events, fail_resume=True)

    with pytest.raises(RuntimeError, match="No conversation found"):
        harness_type(executable=str(cli.path)).run_turn(
            make_turn(workspace, session_id="gone"), lambda _: None
        )


def test_claude_structured_output(tmp_path: Path, workspace: Path) -> None:
    events = claude_events(structured_output=ANSWER)
    events[1] = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Answering."},
                {
                    "type": "tool_use",
                    "id": "t3",
                    "name": "StructuredOutput",
                    "input": {"approved": True},
                },
            ]
        },
    }
    cli = fake_cli(tmp_path, events)
    seen: list[str] = []

    result = ClaudeCode(executable=str(cli.path)).run_turn(
        make_turn(workspace, session_id="sess-1", schema=SCHEMA),
        seen.append,
    )

    assert result.structured == ANSWER
    assert seen == ["Answering."]
    argv = cli.argv()
    schema_arg = argv[argv.index("--json-schema") + 1]
    assert isinstance(schema_arg, str)
    assert json.loads(schema_arg) == SCHEMA
    assert argv[-2:] == ["--resume", "sess-1"]


def test_codex_structured_output(tmp_path: Path, workspace: Path) -> None:
    cli = fake_cli(tmp_path, codex_events(json.dumps(ANSWER)))

    result = Codex(executable=str(cli.path)).run_turn(
        make_turn(workspace, session_id="thread-1", schema=SCHEMA),
        lambda _: None,
    )

    assert result.structured == ANSWER
    argv = cli.argv()
    assert "--output-schema" in argv
    assert argv.index("--output-schema") < argv.index("resume")
    assert argv[-3:] == ["resume", "thread-1", "-"]
    schema_path = argv[argv.index("--output-schema") + 1]
    assert isinstance(schema_path, str)
    assert not Path(schema_path).is_relative_to(workspace)
    assert not Path(schema_path).exists()


@dataclass
class Inner:
    name: str
    count: int | None = None


@dataclass
class Outer:
    inner: Inner
    tags: list[Inner]
    note: str = ""


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (
            TypeAdapter(Outer).json_schema(),
            {
                "$defs": {
                    "Inner": {
                        "properties": {
                            "name": {"title": "Name", "type": "string"},
                            "count": {
                                "anyOf": [{"type": "integer"}, {"type": "null"}],
                                "default": None,
                                "title": "Count",
                            },
                        },
                        "required": ["name", "count"],
                        "additionalProperties": False,
                        "title": "Inner",
                        "type": "object",
                    }
                },
                "properties": {
                    "inner": {"$ref": "#/$defs/Inner"},
                    "tags": {
                        "items": {"$ref": "#/$defs/Inner"},
                        "title": "Tags",
                        "type": "array",
                    },
                    "note": {"default": "", "title": "Note", "type": "string"},
                },
                "required": ["inner", "tags", "note"],
                "additionalProperties": False,
                "title": "Outer",
                "type": "object",
            },
        ),
        (
            {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "object", "properties": {"a": {"type": "string"}}},
                        {"type": "null"},
                    ]
                },
            },
            {
                "type": "array",
                "items": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {"a": {"type": "string"}},
                            "required": ["a"],
                            "additionalProperties": False,
                        },
                        {"type": "null"},
                    ]
                },
            },
        ),
    ],
)
def test_codex_schema_is_made_strict(
    tmp_path: Path,
    workspace: Path,
    schema: dict[str, JsonValue],
    expected: dict[str, JsonValue],
) -> None:
    original = json.loads(json.dumps(schema))
    cli = fake_cli(tmp_path, codex_events("[]"))

    Codex(executable=str(cli.path)).run_turn(
        make_turn(workspace, schema=schema), lambda _: None
    )

    assert cli.call()["schema"] == expected
    assert schema == original


@pytest.mark.parametrize(
    ("harness_type", "events", "exit_code", "error"),
    [
        (ClaudeCode, CLAUDE_OK, 1, "exited with status 1: fake stderr"),
        (
            ClaudeCode,
            [
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "session_id": "sess-1",
                    "result": "Failed to authenticate",
                }
            ],
            1,
            "Failed to authenticate",
        ),
        (
            ClaudeCode,
            [
                {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": True,
                    "session_id": "sess-1",
                }
            ],
            0,
            "error_max_turns",
        ),
        (
            ClaudeCode,
            [
                {
                    "type": "result",
                    "subtype": "error_max_structured_output_retries",
                    "is_error": False,
                    "session_id": "sess-1",
                }
            ],
            0,
            "error_max_structured_output_retries",
        ),
        (ClaudeCode, CLAUDE_OK[:-1], 0, "without a final message"),
        (ClaudeCode, CLAUDE_OK, 0, "without a structured output"),
        (Codex, CODEX_OK, 2, "exited with status 2: fake stderr"),
        (
            Codex,
            [
                CODEX_OK[0],
                {"type": "error", "message": "stream disconnected"},
                {"type": "turn.failed", "error": {"message": "model overloaded"}},
            ],
            1,
            "model overloaded",
        ),
        (
            Codex,
            [CODEX_OK[0], {"type": "error", "message": "stream disconnected"}],
            0,
            "never completed the turn: stream disconnected",
        ),
        (Codex, [CODEX_OK[0], CODEX_OK[-1]], 0, "without a final message"),
        (Codex, CODEX_OK[1:], 0, "without a thread id"),
        (Codex, CODEX_OK, 0, "final message is not JSON: 'All done.'"),
    ],
)
def test_failures_raise(
    tmp_path: Path,
    harness_type: type[ClaudeCode | Codex],
    events: list[dict[str, JsonValue]],
    exit_code: int,
    error: str,
) -> None:
    cli = fake_cli(tmp_path, events, exit_code)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match=error):
        harness_type(executable=str(cli.path)).run_turn(
            make_turn(workspace, schema=SCHEMA), lambda _: None
        )
