import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue, TypeAdapter

from fledge import engine
from fledge.agents import Agent
from fledge.checks import Feedback, Passed, until
from fledge.core import Repo, Run, Sandbox, WorkItem
from fledge.engine import workflow
from fledge.git import GitError
from fledge.github import GitHubIssues
from fledge.steps import clone_repo, command

GITHUB_CREDENTIAL = "ghp_test_credential"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed git argv built by the tests
        ["git", "-c", "user.name=Seeder", "-c", "user.email=seed@example.com", *args],  # noqa: S607 - git from PATH, as in the step image
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's git config (signing, hooks, identity) out of the tests."""
    empty = tmp_path / "empty-gitconfig"
    empty.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def make_origin(tmp_path: Path, name: str, branch: str) -> Path:
    """A bare repository with one commit on `branch`."""
    seed = tmp_path / f"seed-{name}"
    seed.mkdir()
    git(seed, "init", "--initial-branch", branch)
    (seed / "README.md").write_text("hello\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "--message", "initial")
    bare = tmp_path / f"{name}.git"
    git(tmp_path, "clone", "--bare", str(seed), str(bare))
    return bare


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return make_origin(tmp_path, "origin", "main")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@dataclass
class FakeContext:
    workspace: Path
    work: WorkItem = field(
        default_factory=lambda: WorkItem(
            external_id="12",
            title="Add a greeting",
            body="Say hi in the README.",
            url="https://github.com/acme/api/issues/12",
            repo_url="https://github.com/acme/api",
        )
    )
    run_id: UUID = field(default_factory=uuid4)
    agents: dict[str, Agent] = field(default_factory=dict)
    pending_guidance: list[list[str]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    agent_events: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def agent_event(self, message: str) -> None:
        self.agent_events.append(message)

    def claim_guidance(self) -> list[str]:
        return self.pending_guidance.pop(0) if self.pending_guidance else []

    def agent(self, name: str) -> Agent:
        return self.agents[name]


@pytest.mark.parametrize("with_token", [True, False])
def test_clone_repo_checks_out_the_run_branch_without_persisting_the_token(
    monkeypatch: pytest.MonkeyPatch, origin: Path, workspace: Path, with_token: bool
) -> None:
    if with_token:
        monkeypatch.setenv("GITHUB_TOKEN", GITHUB_CREDENTIAL)
    ctx = FakeContext(workspace)

    result = clone_repo.execute(ctx, {"url": str(origin)})

    path = workspace / "repos" / "origin"
    branch = f"fledge/{ctx.run_id.hex[:8]}"
    assert result == {
        "url": str(origin),
        "path": str(path),
        "branch": branch,
        "base": "main",
    }
    assert (path / "README.md").read_text() == "hello\n"
    assert git(path, "branch", "--show-current") == branch
    assert git(path, "remote", "get-url", "origin") == str(origin)
    assert GITHUB_CREDENTIAL not in (path / ".git" / "config").read_text()


def test_clone_repo_detects_a_default_branch_other_than_main(
    tmp_path: Path, workspace: Path
) -> None:
    origin = make_origin(tmp_path, "api", "trunk")

    result = clone_repo.execute(FakeContext(workspace), {"url": str(origin)})

    assert isinstance(result, dict)
    assert result["base"] == "trunk"


def test_clone_repo_puts_each_repository_in_its_own_directory(
    tmp_path: Path, workspace: Path
) -> None:
    ctx = FakeContext(workspace)
    first = make_origin(tmp_path, "api", "main")
    second = make_origin(tmp_path, "web", "main")

    clone_repo.execute(ctx, {"url": str(first)})
    clone_repo.execute(ctx, {"url": str(second)})

    assert sorted(p.name for p in (workspace / "repos").iterdir()) == ["api", "web"]


def test_clone_repo_fails_when_the_directory_is_taken(
    origin: Path, workspace: Path
) -> None:
    ctx = FakeContext(workspace)
    clone_repo.execute(ctx, {"url": str(origin)})

    with pytest.raises(GitError, match="clone"):
        clone_repo.execute(ctx, {"url": str(origin)})


def repo_at(path: Path) -> Repo:
    return Repo("https://github.com/acme/api", path, "fledge/x", "main")


def repo_json(path: Path) -> dict[str, JsonValue]:
    return {
        "url": "https://github.com/acme/api",
        "path": str(path),
        "branch": "fledge/x",
        "base": "main",
    }


def python(code: str) -> list[JsonValue]:
    # Unbuffered, so stdout and stderr interleave in the order they are written.
    return [sys.executable, "-u", "-c", code]


def test_command_passes_in_the_repository(workspace: Path) -> None:
    (workspace / "marker").write_text("")
    argv = python(
        "import pathlib, sys; sys.exit(0 if pathlib.Path('marker').exists() else 3)"
    )

    result = command.step.execute(
        FakeContext(workspace), {"argv": argv, "repo": repo_json(workspace)}
    )

    assert result == {"kind": "passed"}


def test_command_failure_is_feedback_with_the_tail_of_the_output(
    workspace: Path,
) -> None:
    argv = python(
        "import sys\n"
        "for n in range(150): print(f'out {n}')\n"
        "print('err line', file=sys.stderr)\n"
        "sys.exit(4)"
    )

    result = command.step.execute(
        FakeContext(workspace), {"argv": argv, "repo": repo_json(workspace)}
    )

    feedback = TypeAdapter(Passed | Feedback).validate_python(result)
    assert isinstance(feedback, Feedback)
    [item] = feedback.items
    assert item.author == sys.executable
    assert "exited with status 4" in item.body
    tail = item.body.split("End of its output:\n")[1].splitlines()
    assert tail == [f"out {n}" for n in range(51, 150)] + ["err line"]


def test_command_within_a_sandbox_records_the_sandbox(workspace: Path) -> None:
    sandbox = Sandbox(env={"A": "1"})

    @workflow(trigger=GitHubIssues("acme/api", "fledge"))
    def checked(run: Run) -> None:
        for _ in until(
            command.within(sandbox)(["make"], repo_at(workspace)), max_rounds=1
        ):
            pass

    outcome = engine.replay(
        checked, Run(uuid4(), FakeContext(workspace).work), datetime.now(UTC), []
    )

    assert isinstance(outcome, engine.Suspended)
    assert outcome.call.sandbox == sandbox
    assert outcome.call.function == "fledge.steps:command"
    assert outcome.call.args["argv"] == ["make"]
