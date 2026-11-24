import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from fledge.builtin.claude_code import ClaudeCodeHarness
from fledge.builtin.codex import CodexHarness
from fledge.core import Harness

FAKE_CLI = """\
#!{python}
import json, os, sys
from pathlib import Path

here = Path(__file__).parent
(here / "call.json").write_text(json.dumps(
    {{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read()}}
))
sys.stdout.write((here / "stdout.jsonl").read_text())
sys.stderr.write("fake stderr\\n")
sys.exit(int((here / "exit_code").read_text()))
"""


@dataclass
class FakeCli:
    path: Path

    def call(self) -> dict[str, object]:
        recorded: dict[str, object] = json.loads(
            (self.path.parent / "call.json").read_text()
        )
        return recorded


def fake_cli(
    tmp_path: Path, events: list[dict[str, object]], exit_code: int = 0
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
    return FakeCli(script)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


CLAUDE_OK: list[dict[str, object]] = [
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

CODEX_OK: list[dict[str, object]] = [
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


def test_harnesses_satisfy_protocol() -> None:
    assert isinstance(ClaudeCodeHarness(), Harness)
    assert isinstance(CodexHarness(), Harness)


def test_claude_first_turn(tmp_path: Path, workspace: Path) -> None:
    cli = fake_cli(tmp_path, CLAUDE_OK)
    events: list[str] = []

    result = ClaudeCodeHarness(str(cli.path)).run_turn(
        workspace, "-fix the bug", None, events.append
    )

    assert result.session_id == "sess-1"
    assert result.final_message == "All done."
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

    ClaudeCodeHarness(str(cli.path)).run_turn(
        workspace, "more", "sess-1", lambda _: None
    )

    argv = cli.call()["argv"]
    assert isinstance(argv, list)
    assert argv[-2:] == ["--resume", "sess-1"]


def test_codex_first_turn(tmp_path: Path, workspace: Path) -> None:
    cli = fake_cli(tmp_path, CODEX_OK)
    events: list[str] = []

    result = CodexHarness(str(cli.path)).run_turn(
        workspace, "-fix the bug", None, events.append
    )

    assert result.session_id == "thread-1"
    assert result.final_message == "All done."
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


def test_codex_resumed_turn(tmp_path: Path, workspace: Path) -> None:
    cli = fake_cli(tmp_path, CODEX_OK[1:])

    result = CodexHarness(str(cli.path)).run_turn(
        workspace, "more", "thread-1", lambda _: None
    )

    assert result.session_id == "thread-1"
    argv = cli.call()["argv"]
    assert isinstance(argv, list)
    assert argv[-3:] == ["resume", "thread-1", "-"]
    assert argv[argv.index("-C") + 1] == str(workspace)


@pytest.mark.parametrize(
    ("harness_type", "events", "exit_code", "error"),
    [
        (ClaudeCodeHarness, CLAUDE_OK, 1, "exited with status 1: fake stderr"),
        (
            ClaudeCodeHarness,
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
            ClaudeCodeHarness,
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
        (ClaudeCodeHarness, CLAUDE_OK[:-1], 0, "without a final message"),
        (CodexHarness, CODEX_OK, 2, "exited with status 2: fake stderr"),
        (
            CodexHarness,
            [
                CODEX_OK[0],
                {"type": "error", "message": "stream disconnected"},
                {"type": "turn.failed", "error": {"message": "model overloaded"}},
            ],
            1,
            "model overloaded",
        ),
        (
            CodexHarness,
            [CODEX_OK[0], {"type": "error", "message": "stream disconnected"}],
            0,
            "never completed the turn: stream disconnected",
        ),
        (CodexHarness, [CODEX_OK[0], CODEX_OK[-1]], 0, "without a final message"),
        (CodexHarness, CODEX_OK[1:], 0, "without a thread id"),
    ],
)
def test_failures_raise(
    tmp_path: Path,
    harness_type: type[ClaudeCodeHarness | CodexHarness],
    events: list[dict[str, object]],
    exit_code: int,
    error: str,
) -> None:
    cli = fake_cli(tmp_path, events, exit_code)

    with pytest.raises(RuntimeError, match=error):
        harness_type(str(cli.path)).run_turn(tmp_path, "go", None, lambda _: None)
