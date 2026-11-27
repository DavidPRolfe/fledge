import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import JsonValue

from fledge import engine
from fledge.agents import Agent
from fledge.checks import Feedback, FeedbackItem, Passed, Pending, Stopped, until
from fledge.control.store import CallRow
from fledge.core import Repo, Run, WorkItem
from fledge.engine import PendingCall, workflow
from fledge.github import (
    GitHubIssues,
    PullRequest,
    api,
    checks_green,
    mark_ready,
    merged,
    open_pull_request,
    pulls,
    push,
)
from fledge.github.api import MissingTokenError
from fledge.harnesses.base import Harness, Turn, TurnResult

CREDENTIAL = "ghp_test_credential"
REPO = "/repos/acme/api"
SHA = "a" * 40
SINCE = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
AFTER_GRACE = SINCE + timedelta(minutes=10)
BEFORE = "2026-11-01T11:59:00Z"
AFTER = "2026-11-01T12:01:00Z"
LATER = "2026-11-01T12:02:00Z"


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


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", CREDENTIAL)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """A clone of a local origin, on the working branch with one new commit."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch", "main")
    (seed / "README.md").write_text("hello\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "--message", "initial")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "--bare", str(seed), str(origin))
    path = tmp_path / "workspace" / "repos" / "api"
    git(tmp_path, "clone", str(origin), str(path))
    git(path, "switch", "--create", "fledge/abc")
    (path / "README.md").write_text("hello\nhi\n")
    git(path, "commit", "--all", "--message", "greet")
    return Repo("https://github.com/acme/api", path, "fledge/abc", "main")


def pull_request(repo: Repo) -> PullRequest:
    return PullRequest(repo, 5, "https://github.com/acme/api/pull/5")


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
    logs: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def agent_event(self, message: str) -> None:
        self.logs.append(message)

    def claim_guidance(self) -> list[str]:
        return []

    def harness(self, agent: str) -> Harness:
        raise KeyError(agent)


def push_branch(repo: Repo) -> None:
    git(repo.path, "push", "origin", f"HEAD:refs/heads/{repo.branch}")


def no_change(repo: Repo) -> None:
    del repo


def make_dirty(repo: Repo) -> None:
    push_branch(repo)
    (repo.path / "new.txt").write_text("new\n")


def commit_after_push(repo: Repo) -> None:
    push_branch(repo)
    (repo.path / "README.md").write_text("bye\n")
    git(repo.path, "commit", "--all", "--message", "more")


@pytest.mark.usefixtures("token")
@pytest.mark.parametrize(
    ("prepare", "error"),
    [
        (no_change, "fledge/abc does not exist on origin"),
        (make_dirty, "uncommitted changes:\n\\?\\? new.txt"),
        (commit_after_push, "fledge/abc on origin is at .*, but the local HEAD"),
    ],
)
def test_verify_pushed_fails_naming_the_condition(
    repo: Repo, prepare: Callable[[Repo], None], error: str
) -> None:
    prepare(repo)

    with pytest.raises(RuntimeError, match=error):
        pulls.verify_pushed.execute(FakeContext(repo.path), {"repo": repo_json(repo)})


@pytest.mark.usefixtures("token")
def test_verify_pushed_passes_when_origin_has_the_local_head(repo: Repo) -> None:
    push_branch(repo)

    assert (
        pulls.verify_pushed.execute(FakeContext(repo.path), {"repo": repo_json(repo)})
        is None
    )


def test_verify_pushed_needs_a_token(repo: Repo) -> None:
    push_branch(repo)

    with pytest.raises(MissingTokenError, match="GITHUB_TOKEN"):
        pulls.verify_pushed.execute(FakeContext(repo.path), {"repo": repo_json(repo)})


def repo_json(repo: Repo) -> dict[str, JsonValue]:
    return {
        "url": repo.url,
        "path": str(repo.path),
        "branch": repo.branch,
        "base": repo.base,
    }


type Json = dict[str, object]


def check_run(  # noqa: PLR0913 - one argument per field the tests vary
    id_: int,
    name: str,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    app: str = "github-actions",
    title: str | None = None,
    summary: str | None = None,
) -> Json:
    return {
        "id": id_,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "details_url": f"https://github.com/acme/api/runs/{id_}",
        "output": {"title": title, "summary": summary},
        "app": {"slug": app},
    }


def status(context: str, state: str) -> Json:
    return {
        "context": context,
        "state": state,
        "description": f"{context} said {state}",
        "target_url": f"https://ci.example.com/{context}",
    }


def user(login: str, kind: str = "User") -> Json:
    return {"login": login, "type": kind}


def issue_comment(login: str, body: str, at: str, kind: str = "User") -> Json:
    return {"id": 1, "user": user(login, kind), "body": body, "created_at": at}


def review(login: str, state: str, body: str, at: str | None) -> Json:
    return {"user": user(login), "body": body, "state": state, "submitted_at": at}


def review_comment(login: str, body: str, at: str, line: int | None) -> Json:
    return {
        "user": user(login),
        "body": body,
        "path": "src/app.py",
        "line": line,
        "original_line": 3,
        "created_at": at,
    }


LOG_HOST = "https://results.example.com"


@dataclass
class FakeGitHub:
    """GitHub's REST and GraphQL APIs for pull request 5 of acme/api."""

    state: str = "open"
    merged_at: str | None = None
    draft: bool = False
    open_pulls: list[Json] = field(default_factory=list)
    check_runs: list[Json] = field(default_factory=list)
    statuses: list[Json] = field(default_factory=list)
    checks_forbidden: bool = False
    """Whether the token is one GitHub refuses check runs to."""
    workflow_runs: list[Json] = field(default_factory=list)
    jobs: dict[int, list[Json]] = field(default_factory=dict)
    """GitHub Actions jobs by workflow run id."""
    logs: dict[int, str | int] = field(default_factory=dict)
    """A job's log text, or the status code its log request gets."""
    reviews: list[Json] = field(default_factory=list)
    review_comments: list[Json] = field(default_factory=list)
    issue_comments: list[Json] = field(default_factory=list)
    graphql_errors: list[Json] = field(default_factory=list)
    login: str = "fledge-bot"
    page_size: int = 100
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "results.example.com":
            return httpx.Response(200, text=str(self.logs[int(path.split("/")[-1])]))
        if path.startswith(f"{REPO}/actions/jobs/"):
            return self._job_log(int(path.split("/")[-2]))
        if path == "/graphql":
            return httpx.Response(200, json={"data": {}, "errors": self.graphql_errors})
        if self.checks_forbidden and path.endswith("/check-runs"):
            return httpx.Response(403, json={"message": "Resource not accessible"})
        paged: dict[str, tuple[list[Json], Callable[[list[Json]], Json]]] = {
            f"{REPO}/actions/runs": (
                self.workflow_runs,
                lambda page: {"workflow_runs": page},
            ),
            **{
                f"{REPO}/actions/runs/{run_id}/jobs": (
                    jobs,
                    lambda page: {"jobs": page},
                )
                for run_id, jobs in self.jobs.items()
            },
            f"{REPO}/commits/{SHA}/check-runs": (
                self.check_runs,
                lambda page: {"total_count": len(self.check_runs), "check_runs": page},
            ),
            f"{REPO}/commits/{SHA}/status": (
                self.statuses,
                lambda page: {"state": "pending", "statuses": page},
            ),
        }
        if path in paged:
            return self._page(request, *paged[path])
        bodies: dict[str, Json | list[Json]] = {
            f"{REPO}/pulls": self.open_pulls,
            f"{REPO}/pulls/5": self._pull(),
            f"{REPO}/pulls/5/reviews": self.reviews,
            f"{REPO}/pulls/5/comments": self.review_comments,
            f"{REPO}/issues/5/comments": self.issue_comments,
            "/user": user(self.login),
        }
        return httpx.Response(200, json=bodies[path])

    def _job_log(self, job_id: int) -> httpx.Response:
        log = self.logs[job_id]
        if isinstance(log, int):
            return httpx.Response(log)
        return httpx.Response(302, headers={"Location": f"{LOG_HOST}/logs/{job_id}"})

    def _page(
        self,
        request: httpx.Request,
        items: list[Json],
        wrap: Callable[[list[Json]], Json],
    ) -> httpx.Response:
        number = int(request.url.params.get("page", "1"))
        start = (number - 1) * self.page_size
        headers = {}
        if start + self.page_size < len(items):
            next_url = request.url.copy_set_param("page", str(number + 1))
            headers["Link"] = f'<{next_url}>; rel="next"'
        return httpx.Response(
            200, json=wrap(items[start : start + self.page_size]), headers=headers
        )

    def _pull(self) -> Json:
        return {
            "number": 5,
            "html_url": "https://github.com/acme/api/pull/5",
            "node_id": "PR_node5",
            "state": self.state,
            "draft": self.draft,
            "merged_at": self.merged_at,
            "head": {"sha": SHA},
        }

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(self.handle),
        )

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def open_pull(number: int) -> Json:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/api/pull/{number}",
        "node_id": f"PR_node{number}",
        "state": "open",
        "draft": True,
        "merged_at": None,
        "head": {"sha": SHA},
    }


def test_find_pull_request_returns_the_one_open_pull_request(repo: Repo) -> None:
    github = FakeGitHub(open_pulls=[open_pull(5)])

    found = pulls.find_pull_request(github.client(), repo)

    assert found == PullRequest(repo, 5, "https://github.com/acme/api/pull/5")
    [request] = github.requests
    assert request.url.path == f"{REPO}/pulls"
    assert request.url.params["head"] == "acme:fledge/abc"
    assert request.url.params["state"] == "open"


@pytest.mark.parametrize("count", [0, 2])
def test_find_pull_request_fails_unless_exactly_one_is_open(
    repo: Repo, count: int
) -> None:
    github = FakeGitHub(open_pulls=[open_pull(n) for n in range(count)])

    with pytest.raises(RuntimeError, match=f"found {count}"):
        pulls.find_pull_request(github.client(), repo)


def use_fake_github(monkeypatch: pytest.MonkeyPatch, github: FakeGitHub) -> list[str]:
    """Route the steps' and waits' GitHub client to the fake; returns tokens used."""
    tokens: list[str] = []

    def client(token: str) -> httpx.Client:
        tokens.append(token)
        return github.client()

    monkeypatch.setattr(api, "client", client)
    return tokens


@pytest.mark.usefixtures("token")
def test_verify_opened_checks_the_push_then_finds_the_pull_request(
    monkeypatch: pytest.MonkeyPatch, repo: Repo
) -> None:
    push_branch(repo)
    github = FakeGitHub(open_pulls=[open_pull(5)])
    tokens = use_fake_github(monkeypatch, github)

    result = pulls.verify_opened.execute(
        FakeContext(repo.path), {"repo": repo_json(repo)}
    )

    assert result == {
        "repo": repo_json(repo),
        "number": 5,
        "url": "https://github.com/acme/api/pull/5",
    }
    assert tokens == [CREDENTIAL]


@pytest.mark.usefixtures("token")
def test_verify_opened_fails_before_asking_github_when_not_pushed(
    monkeypatch: pytest.MonkeyPatch, repo: Repo
) -> None:
    github = FakeGitHub(open_pulls=[open_pull(5)])
    use_fake_github(monkeypatch, github)

    with pytest.raises(RuntimeError, match="does not exist on origin"):
        pulls.verify_opened.execute(FakeContext(repo.path), {"repo": repo_json(repo)})
    assert github.requests == []


@pytest.mark.usefixtures("token")
def test_mark_ready_marks_a_draft_ready_with_graphql(
    monkeypatch: pytest.MonkeyPatch, repo: Repo
) -> None:
    github = FakeGitHub(draft=True)
    use_fake_github(monkeypatch, github)
    pr = pull_request(repo)

    mark_ready.execute(
        FakeContext(repo.path),
        {"pr": {"repo": repo_json(repo), "number": 5, "url": pr.url}},
    )

    assert github.paths() == [f"{REPO}/pulls/5", "/graphql"]
    body = json.loads(github.requests[1].content)
    assert "markPullRequestReadyForReview" in body["query"]
    assert body["variables"] == {"id": "PR_node5"}


def test_mark_ready_leaves_a_ready_pull_request_alone(repo: Repo) -> None:
    github = FakeGitHub(draft=False)

    pulls.ready_for_review(github.client(), pull_request(repo))

    assert github.paths() == [f"{REPO}/pulls/5"]


def test_mark_ready_raises_graphql_errors(repo: Repo) -> None:
    github = FakeGitHub(draft=True, graphql_errors=[{"message": "not allowed"}])

    with pytest.raises(RuntimeError, match="not allowed"):
        pulls.ready_for_review(github.client(), pull_request(repo))


LOG = "\n".join(f"log line {n}" for n in range(60))


@pytest.mark.parametrize(
    ("github", "now", "expected"),
    [
        pytest.param(
            FakeGitHub(merged_at=AFTER, state="closed"),
            SINCE,
            Passed(),
            id="merged",
        ),
        pytest.param(FakeGitHub(state="closed"), SINCE, Stopped(), id="closed"),
        pytest.param(
            FakeGitHub(
                check_runs=[
                    check_run(1, "lint"),
                    check_run(2, "test", status="in_progress"),
                ]
            ),
            SINCE,
            Pending(),
            id="check-run-in-progress",
        ),
        pytest.param(
            FakeGitHub(check_runs=[check_run(1, "lint", status="queued")]),
            SINCE,
            Pending(),
            id="check-run-queued",
        ),
        pytest.param(
            FakeGitHub(statuses=[status("ci/legacy", "pending")]),
            SINCE,
            Pending(),
            id="status-pending",
        ),
        pytest.param(
            FakeGitHub(
                check_runs=[
                    check_run(1, "lint"),
                    check_run(2, "docs", conclusion="skipped"),
                    check_run(3, "deploy", conclusion="neutral"),
                ],
                statuses=[status("ci/legacy", "success")],
            ),
            SINCE,
            Passed(),
            id="all-green",
        ),
        pytest.param(FakeGitHub(), SINCE, Pending(), id="no-checks-yet"),
        pytest.param(
            FakeGitHub(),
            SINCE + timedelta(minutes=1, seconds=59),
            Pending(),
            id="no-checks-within-grace",
        ),
        pytest.param(
            FakeGitHub(),
            SINCE + pulls.NO_CHECKS_GRACE,
            Passed(),
            id="no-checks-after-grace",
        ),
        pytest.param(
            FakeGitHub(statuses=[status("ci/legacy", "error")]),
            SINCE,
            Feedback(
                [
                    FeedbackItem(
                        "ci/legacy",
                        "ci/legacy reported error.\n\nci/legacy said error\n\n"
                        "Details: https://ci.example.com/ci/legacy",
                    )
                ]
            ),
            id="status-error",
        ),
        pytest.param(
            FakeGitHub(
                check_runs=[
                    check_run(
                        4,
                        "Vercel",
                        conclusion="action_required",
                        app="vercel",
                        title="Authorize",
                    )
                ]
            ),
            SINCE,
            Feedback(
                [
                    FeedbackItem(
                        "Vercel",
                        "Vercel concluded action_required.\n\nAuthorize\n\n"
                        "Details: https://github.com/acme/api/runs/4",
                    )
                ]
            ),
            id="non-actions-failure-has-no-log",
        ),
    ],
)
def test_checks_green(
    repo: Repo, github: FakeGitHub, now: datetime, expected: object
) -> None:
    assert pulls.ci_verdict(github.client(), pull_request(repo), SINCE, now) == expected


def job(id_: int, name: str, *, status: str = "completed", conclusion: str) -> Json:
    return {
        "id": id_,
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "html_url": f"https://github.com/acme/api/actions/runs/1/job/{id_}",
    }


@pytest.mark.parametrize(
    ("workflow_runs", "jobs", "now", "expected"),
    [
        pytest.param([], {}, SINCE, Pending(), id="nothing-yet"),
        pytest.param([], {}, SINCE + pulls.NO_CHECKS_GRACE, Passed(), id="no-ci"),
        pytest.param(
            [{"id": 1, "status": "queued"}],
            {1: []},
            AFTER_GRACE,
            Pending(),
            id="queued",
        ),
        pytest.param(
            [{"id": 1, "status": "completed"}],
            {1: [job(7, "test", conclusion="success")]},
            SINCE,
            Passed(),
            id="passed",
        ),
        pytest.param(
            [{"id": 1, "status": "completed"}],
            {1: [job(7, "test", conclusion="failure")]},
            SINCE,
            Feedback(
                [
                    FeedbackItem(
                        "test",
                        "test concluded failure.\n\n"
                        "Details: https://github.com/acme/api/actions/runs/1/job/7\n\n"
                        "End of the job's log:\nboom",
                    )
                ]
            ),
            id="failed-with-log",
        ),
    ],
)
def test_checks_green_reads_actions_jobs_when_check_runs_are_refused(
    repo: Repo,
    workflow_runs: list[Json],
    jobs: dict[int, list[Json]],
    now: datetime,
    expected: object,
) -> None:
    github = FakeGitHub(
        checks_forbidden=True, workflow_runs=workflow_runs, jobs=jobs, logs={7: "boom"}
    )
    assert pulls.ci_verdict(github.client(), pull_request(repo), SINCE, now) == expected


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "timed_out", "cancelled", "action_required", "startup_failure"],
)
def test_checks_green_reports_a_failure_with_its_log_tail_without_waiting(
    repo: Repo, conclusion: str
) -> None:
    github = FakeGitHub(
        check_runs=[
            check_run(
                7, "test", conclusion=conclusion, title="2 failed", summary="see log"
            ),
            check_run(8, "slow", status="in_progress"),
        ],
        logs={7: LOG},
    )

    verdict = pulls.ci_verdict(github.client(), pull_request(repo), SINCE, SINCE)

    assert isinstance(verdict, Feedback)
    [item] = verdict.items
    assert item.author == "test"
    assert item.body.startswith(
        f"test concluded {conclusion}.\n\n2 failed\n\nsee log\n\n"
        "Details: https://github.com/acme/api/runs/7\n\nEnd of the job's log:\n"
    )
    tail = item.body.split("End of the job's log:\n")[1].splitlines()
    assert tail == [f"log line {n}" for n in range(10, 60)]
    log_requests = [r for r in github.requests if "logs" in r.url.path]
    assert [str(r.url) for r in log_requests] == [
        f"https://api.github.com{REPO}/actions/jobs/7/logs",
        f"{LOG_HOST}/logs/7",
    ]
    assert "Authorization" not in log_requests[1].headers


@pytest.mark.parametrize("code", [404, 410])
def test_checks_green_says_when_the_log_is_gone(repo: Repo, code: int) -> None:
    github = FakeGitHub(
        check_runs=[check_run(7, "test", conclusion="failure")], logs={7: code}
    )

    verdict = pulls.ci_verdict(github.client(), pull_request(repo), SINCE, SINCE)

    assert verdict == Feedback(
        [
            FeedbackItem(
                "test",
                "test concluded failure.\n\n"
                "Details: https://github.com/acme/api/runs/7\n\n"
                "The job's log is no longer available.",
            )
        ]
    )


def test_checks_green_raises_other_log_errors(repo: Repo) -> None:
    github = FakeGitHub(
        check_runs=[check_run(7, "test", conclusion="failure")], logs={7: 500}
    )

    with pytest.raises(httpx.HTTPStatusError):
        pulls.ci_verdict(github.client(), pull_request(repo), SINCE, SINCE)


def test_checks_green_reads_every_page_of_check_runs(repo: Repo) -> None:
    github = FakeGitHub(
        check_runs=[check_run(1, "lint"), check_run(2, "test", conclusion="failure")],
        logs={2: "boom"},
        page_size=1,
    )

    verdict = pulls.ci_verdict(github.client(), pull_request(repo), SINCE, SINCE)

    assert isinstance(verdict, Feedback)
    assert [item.author for item in verdict.items] == ["test"]


@pytest.mark.parametrize(
    ("github", "expected"),
    [
        pytest.param(
            FakeGitHub(merged_at=AFTER, state="closed"), Passed(), id="merged"
        ),
        pytest.param(FakeGitHub(state="closed"), Stopped(), id="closed-unmerged"),
        pytest.param(FakeGitHub(), Pending(), id="nothing-new"),
        pytest.param(
            FakeGitHub(issue_comments=[issue_comment("alice", "Rename it", AFTER)]),
            Feedback([FeedbackItem("alice", "Rename it")]),
            id="new-human-comment",
        ),
        pytest.param(
            FakeGitHub(
                issue_comments=[
                    issue_comment("fledge-bot", "Done", AFTER),
                    issue_comment("dependabot[bot]", "Bump", AFTER, kind="Bot"),
                ]
            ),
            Pending(),
            id="own-and-bot-comments-ignored",
        ),
        pytest.param(
            FakeGitHub(
                issue_comments=[
                    issue_comment("alice", "old", BEFORE),
                    issue_comment("alice", "exactly then", "2026-11-01T12:00:00Z"),
                ]
            ),
            Pending(),
            id="old-comments-ignored",
        ),
        pytest.param(
            FakeGitHub(
                reviews=[
                    review("bob", "CHANGES_REQUESTED", "", AFTER),
                    review("carol", "APPROVED", "", AFTER),
                    review("dave", "APPROVED", "Nice, one nit", AFTER),
                    review("erin", "COMMENTED", "", AFTER),
                    review("frank", "PENDING", "draft", None),
                    review("gina", "CHANGES_REQUESTED", "old", BEFORE),
                ]
            ),
            Feedback(
                [
                    FeedbackItem("bob", "Changes requested."),
                    FeedbackItem("dave", "Nice, one nit"),
                ]
            ),
            id="reviews",
        ),
        pytest.param(
            FakeGitHub(
                review_comments=[
                    review_comment("bob", "Off by one", AFTER, 12),
                    review_comment("bob", "Outdated", AFTER, None),
                    review_comment("bob", "Old", BEFORE, 1),
                ]
            ),
            Feedback(
                [
                    FeedbackItem("bob", "On src/app.py line 12:\nOff by one"),
                    FeedbackItem("bob", "On src/app.py line 3:\nOutdated"),
                ]
            ),
            id="review-comments-name-file-and-line",
        ),
        pytest.param(
            FakeGitHub(
                issue_comments=[issue_comment("alice", "second", LATER)],
                reviews=[review("bob", "COMMENTED", "first", AFTER)],
                check_runs=[check_run(3, "ci", conclusion="failure", app="other")],
            ),
            Feedback(
                [
                    FeedbackItem("bob", "first"),
                    FeedbackItem("alice", "second"),
                    FeedbackItem(
                        "ci",
                        "ci concluded failure.\n\n"
                        "Details: https://github.com/acme/api/runs/3",
                    ),
                ]
            ),
            id="in-order-then-failing-checks",
        ),
    ],
)
def test_merged(repo: Repo, github: FakeGitHub, expected: object) -> None:
    assert pulls.review_verdict(github.client(), pull_request(repo), SINCE) == expected


def test_merged_asks_for_comments_since_the_loop_last_had_feedback(repo: Repo) -> None:
    github = FakeGitHub()

    pulls.review_verdict(github.client(), pull_request(repo), SINCE)

    comment_requests = [r for r in github.requests if r.url.path.endswith("comments")]
    assert len(comment_requests) == 2
    assert all(r.url.params["since"] == SINCE.isoformat() for r in comment_requests)


@dataclass(frozen=True)
class UnusedHarness:
    """Replay never runs a turn: the journal has the results."""

    def run_turn(self, turn: Turn, on_event: Callable[[str], None]) -> TurnResult:
        raise AssertionError(f"a replay ran a turn: {turn.prompt}")


implementer = Agent("implementer", UnusedHarness())
WORK = WorkItem(
    external_id="12",
    title="Add a greeting",
    body="Say hi in the README.",
    url="https://github.com/acme/api/issues/12",
    repo_url="https://github.com/acme/api",
)


def entry(run: Run, call: PendingCall, result: JsonValue) -> CallRow:
    at = SINCE + timedelta(minutes=call.position)
    return CallRow(
        id=uuid4(),
        run_id=run.id,
        position=call.position,
        kind=call.kind,
        name=call.name,
        function=call.function,
        args=call.args,
        status="succeeded",
        result=result,
        error=None,
        token_hash="",
        started_at=at,
        finished_at=at,
    )


def test_open_pull_request_and_push_prompt_the_agent_then_verify(
    tmp_path: Path,
) -> None:
    repo = Repo("https://github.com/acme/api", tmp_path, "fledge/abc", "trunk")
    pr = PullRequest(repo, 5, "https://github.com/acme/api/pull/5")
    finished: list[PullRequest] = []

    @workflow(trigger=GitHubIssues("acme/api", "fledge"))
    def publish(run: Run) -> None:
        opened = open_pull_request(implementer, repo, draft=True)
        push(implementer, opened)
        finished.append(opened)

    run = Run(uuid4(), WORK)
    results: list[JsonValue] = [
        {"message": "Opened it.", "structured": None},
        {"repo": repo_json(repo), "number": 5, "url": pr.url},
        {"message": "Pushed.", "structured": None},
        None,
    ]
    journal: list[CallRow] = []
    calls: list[PendingCall] = []
    for result in results:
        outcome = engine.replay(publish, run, SINCE, journal)
        assert isinstance(outcome, engine.Suspended)
        calls.append(outcome.call)
        journal.append(entry(run, outcome.call, result))

    assert engine.replay(publish, run, SINCE, journal) == engine.Succeeded()
    assert finished[-1] == pr
    assert [(c.kind, c.name, c.function) for c in calls] == [
        ("step", "implementer", "fledge.agents:run_turn"),
        ("step", "verify_opened", "fledge.github.pulls:verify_opened"),
        ("step", "implementer", "fledge.agents:run_turn"),
        ("step", "verify_pushed", "fledge.github.pulls:verify_pushed"),
    ]
    assert calls[1].args == {"repo": repo_json(repo)}
    assert calls[3].args == {"repo": repo_json(repo)}
    open_prompt = calls[0].args["prompt"]
    assert isinstance(open_prompt, str)
    assert "fledge/abc" in open_prompt
    assert "trunk" in open_prompt
    assert "--draft" in open_prompt
    assert WORK.url in open_prompt
    push_prompt = calls[2].args["prompt"]
    assert isinstance(push_prompt, str)
    assert "fledge/abc" in push_prompt
    assert "merge" in push_prompt


def test_open_pull_request_without_draft_does_not_ask_for_one(tmp_path: Path) -> None:
    repo = Repo("https://github.com/acme/api", tmp_path, "fledge/abc", "main")

    @workflow(trigger=GitHubIssues("acme/api", "fledge"))
    def publish(run: Run) -> None:
        open_pull_request(implementer, repo)

    outcome = engine.replay(publish, Run(uuid4(), WORK), SINCE, [])

    assert isinstance(outcome, engine.Suspended)
    prompt = outcome.call.args["prompt"]
    assert isinstance(prompt, str)
    assert "draft" not in prompt


@pytest.mark.usefixtures("token")
def test_checks_green_wait_polls_github_with_the_token(
    monkeypatch: pytest.MonkeyPatch, repo: Repo
) -> None:
    github = FakeGitHub(check_runs=[check_run(1, "lint", status="queued")])
    tokens = use_fake_github(monkeypatch, github)
    pr = pull_request(repo)

    @workflow(trigger=GitHubIssues("acme/api", "fledge"))
    def gated(run: Run) -> None:
        for _ in until(checks_green(pr), max_rounds=1):
            pass

    outcome = engine.replay(gated, Run(uuid4(), WORK), SINCE, [])

    assert isinstance(outcome, engine.Suspended)
    poll = outcome.call.poll
    assert poll is not None
    assert outcome.call.kind == "wait"
    assert outcome.call.args["since"] == SINCE.isoformat()
    assert poll(SINCE) is None
    github.check_runs = [check_run(1, "lint")]
    assert poll(SINCE) == {"kind": "passed"}
    assert tokens == [CREDENTIAL, CREDENTIAL]


def test_merged_wait_needs_a_token(repo: Repo) -> None:
    pr = pull_request(repo)

    @workflow(trigger=GitHubIssues("acme/api", "fledge"))
    def gated(run: Run) -> None:
        for _ in until(merged(pr), max_rounds=1):
            pass

    outcome = engine.replay(gated, Run(uuid4(), WORK), SINCE, [])

    assert isinstance(outcome, engine.Suspended)
    poll = outcome.call.poll
    assert poll is not None
    with pytest.raises(MissingTokenError, match="GITHUB_TOKEN"):
        poll(SINCE)


@pytest.mark.usefixtures("token")
def test_checks_green_gives_a_late_push_the_whole_grace_period(
    monkeypatch: pytest.MonkeyPatch, repo: Repo
) -> None:
    github = FakeGitHub()
    use_fake_github(monkeypatch, github)
    pr = pull_request(repo)

    @workflow(trigger=GitHubIssues("acme/api", "fledge"))
    def gated(run: Run) -> None:
        for _ in until(checks_green(pr), max_rounds=1):
            pass

    outcome = engine.replay(gated, Run(uuid4(), WORK), SINCE, [])

    assert isinstance(outcome, engine.Suspended)
    poll = outcome.call.poll
    assert poll is not None
    # The loop last had feedback long ago (SINCE), but this wait only just
    # began, so CI that has not registered yet is still worth waiting for.
    assert poll(datetime.now(UTC)) is None
    assert poll(SINCE) == {"kind": "passed"}
