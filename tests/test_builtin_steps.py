import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from fledge.builtin.steps.agent import AgentStep
from fledge.builtin.steps.clone_repo import CloneRepo
from fledge.builtin.steps.open_pull_request import OpenPullRequest
from fledge.builtin.workflows.ticket_to_pr import ticket_to_pr
from fledge.core import Harness, TurnResult, WorkItem

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
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repository with one commit on main."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch", "main")
    (seed / "README.md").write_text("hello\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "--message", "initial")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "--bare", str(seed), str(bare))
    return bare


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@dataclass
class FakeContext:
    workspace: Path
    work: WorkItem
    run_id: UUID = field(default_factory=uuid4)
    outputs: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    pending_guidance: list[list[str]] = field(default_factory=list)
    harnesses: dict[str, Harness] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    agent_events: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def agent_event(self, message: str) -> None:
        self.agent_events.append(message)

    def claim_guidance(self) -> list[str]:
        return self.pending_guidance.pop(0) if self.pending_guidance else []

    def harness(self, name: str) -> Harness:
        return self.harnesses[name]


def work_item(repo_url: str) -> WorkItem:
    return WorkItem(
        external_id="12",
        title="Add a greeting",
        body="Say hi in the README.",
        url="https://github.com/acme/api/issues/12",
        repo_url=repo_url,
    )


@pytest.mark.parametrize("with_token", [True, False])
def test_clone_repo_clones_without_persisting_the_token(
    monkeypatch: pytest.MonkeyPatch, origin: Path, workspace: Path, with_token: bool
) -> None:
    if with_token:
        monkeypatch.setenv("GITHUB_TOKEN", GITHUB_CREDENTIAL)
    ctx = FakeContext(workspace=workspace, work=work_item(str(origin)))

    assert CloneRepo().run(ctx) == {}

    assert (workspace / "README.md").read_text() == "hello\n"
    assert git(workspace, "remote", "get-url", "origin") == str(origin)
    assert GITHUB_CREDENTIAL not in (workspace / ".git" / "config").read_text()


@dataclass
class FakeHarness:
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def run_turn(
        self,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        on_event: Callable[[str], None],
    ) -> TurnResult:
        self.calls.append((prompt, session_id))
        turn = len(self.calls)
        on_event(f"working on turn {turn}")
        return TurnResult(session_id=f"session-{turn}", final_message=f"done {turn}")


def run_agent_step(
    workspace: Path, pending_guidance: list[list[str]]
) -> tuple[dict[str, str], FakeHarness, FakeContext]:
    harness = FakeHarness()
    ctx = FakeContext(
        workspace=workspace,
        work=work_item("https://github.com/acme/api"),
        pending_guidance=pending_guidance,
        harnesses={"fake": harness},
    )
    step = AgentStep(name="implement", harness="fake", prompt="{title}|{body}|{url}")
    return step.run(ctx), harness, ctx


def test_agent_step_without_guidance_runs_one_turn(workspace: Path) -> None:
    outputs, harness, ctx = run_agent_step(workspace, [])

    assert harness.calls == [
        (
            "Add a greeting|Say hi in the README.|https://github.com/acme/api/issues/12",
            None,
        )
    ]
    assert outputs == {"summary": "done 1"}
    assert ctx.agent_events == ["working on turn 1"]


def test_agent_step_delivers_guidance_in_the_same_session(workspace: Path) -> None:
    outputs, harness, ctx = run_agent_step(
        workspace, [["use tabs", "add a test"], [], ["too late"]]
    )

    assert len(harness.calls) == 2
    guidance_prompt, session_id = harness.calls[1]
    assert session_id == "session-1"
    assert "Message 1:\nuse tabs" in guidance_prompt
    assert "Message 2:\nadd a test" in guidance_prompt
    assert outputs == {"summary": "done 2"}
    assert ctx.agent_events == ["working on turn 1", "working on turn 2"]
    assert ctx.pending_guidance == [["too late"]]


def test_agent_step_keeps_going_while_guidance_arrives(workspace: Path) -> None:
    outputs, harness, _ = run_agent_step(workspace, [["one"], ["two"]])

    assert [session for _, session in harness.calls] == [
        None,
        "session-1",
        "session-2",
    ]
    assert outputs == {"summary": "done 3"}


@dataclass
class FakeGitHub:
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            201, json={"html_url": "https://github.com/acme/api/pull/5"}
        )


def cloned(origin: Path, workspace: Path) -> Path:
    git(workspace, "clone", str(origin), str(workspace))
    return workspace


@pytest.mark.parametrize("agent_committed", [True, False])
@pytest.mark.parametrize(
    "repo_url", ["https://github.com/acme/api", "https://github.com/acme/api.git"]
)
def test_open_pull_request_pushes_branch_and_opens_pr(
    monkeypatch: pytest.MonkeyPatch,
    origin: Path,
    workspace: Path,
    agent_committed: bool,
    repo_url: str,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_CREDENTIAL)
    cloned(origin, workspace)
    (workspace / "README.md").write_text("hello\nhi\n")
    if agent_committed:
        git(workspace, "commit", "--all", "--message", "agent work")
    ctx = FakeContext(
        workspace=workspace,
        work=work_item(repo_url),
        outputs={"clone": {}, "implement": {"summary": "Added a greeting."}},
    )
    github = FakeGitHub()

    outputs = OpenPullRequest(transport=httpx.MockTransport(github.handle)).run(ctx)

    branch = f"fledge/{ctx.run_id.hex[:8]}"
    pushed_sha = git(origin, "rev-parse", f"refs/heads/{branch}")
    assert outputs == {
        "pull_request_url": "https://github.com/acme/api/pull/5",
        "head_sha": pushed_sha,
    }
    assert git(origin, "show", f"{pushed_sha}:README.md") == "hello\nhi"
    expected_author = "Seeder" if agent_committed else "fledge"
    assert git(origin, "log", "-1", "--format=%an", pushed_sha) == expected_author
    assert git(workspace, "status", "--porcelain") == ""
    assert GITHUB_CREDENTIAL not in (workspace / ".git" / "config").read_text()

    [request] = github.requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.github.com/repos/acme/api/pulls"
    assert request.headers["Authorization"] == f"Bearer {GITHUB_CREDENTIAL}"
    assert json.loads(request.content) == {
        "title": "Add a greeting",
        "head": branch,
        "base": "main",
        "body": "Resolves https://github.com/acme/api/issues/12\n\nAdded a greeting.",
    }


def test_open_pull_request_body_without_summary_links_the_ticket(
    monkeypatch: pytest.MonkeyPatch, origin: Path, workspace: Path
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_CREDENTIAL)
    cloned(origin, workspace)
    (workspace / "new.txt").write_text("new\n")
    ctx = FakeContext(
        workspace=workspace, work=work_item("https://github.com/acme/api")
    )
    github = FakeGitHub()

    OpenPullRequest(transport=httpx.MockTransport(github.handle)).run(ctx)

    [request] = github.requests
    assert json.loads(request.content)["body"] == (
        "Resolves https://github.com/acme/api/issues/12"
    )


def test_open_pull_request_fails_when_there_is_no_change(
    monkeypatch: pytest.MonkeyPatch, origin: Path, workspace: Path
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_CREDENTIAL)
    cloned(origin, workspace)
    ctx = FakeContext(
        workspace=workspace, work=work_item("https://github.com/acme/api")
    )
    github = FakeGitHub()

    with pytest.raises(RuntimeError, match="no changes to propose"):
        OpenPullRequest(transport=httpx.MockTransport(github.handle)).run(ctx)

    assert git(origin, "branch", "--list", "fledge/*") == ""
    assert github.requests == []


def test_open_pull_request_requires_a_token(origin: Path, workspace: Path) -> None:
    cloned(origin, workspace)
    (workspace / "new.txt").write_text("new\n")
    ctx = FakeContext(
        workspace=workspace, work=work_item("https://github.com/acme/api")
    )

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        OpenPullRequest().run(ctx)

    assert git(origin, "branch", "--list", "fledge/*") == ""


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://gitlab.com/acme/api",
        "git@github.com:acme/api.git",
        "https://github.com/acme",
    ],
)
def test_open_pull_request_rejects_non_github_urls(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, repo_url: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_CREDENTIAL)
    ctx = FakeContext(workspace=workspace, work=work_item(repo_url))

    with pytest.raises(ValueError, match="not a GitHub repository URL"):
        OpenPullRequest().run(ctx)


def test_ticket_to_pr_prompt_formats_with_work_item_fields() -> None:
    [_, implement, _] = ticket_to_pr.steps
    assert isinstance(implement, AgentStep)

    prompt = implement.prompt.format(title="T", body="B {not a field}", url="U")

    assert "T" in prompt
    assert "B {not a field}" in prompt
