"""Pull requests: an agent opens and updates them, Fledge verifies and watches them.

The functions that talk to GitHub take an httpx.Client, so that tests can give
them one with a mock transport; the steps and waits around them build the
real client from GITHUB_TOKEN.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from pydantic import BaseModel, Field, TypeAdapter

from fledge import engine
from fledge.agents import Agent
from fledge.checks import (
    Feedback,
    FeedbackItem,
    Passed,
    Pending,
    Stopped,
    Verdict,
    WaitContext,
    wait,
)
from fledge.core import Repo, StepContext
from fledge.engine import step
from fledge.git import git
from fledge.github import api

logger = logging.getLogger(__name__)

NO_CHECKS_GRACE = timedelta(minutes=2)
"""How long after a wait begins a pull request may have no checks at all
before it counts as having none, rather than CI not having registered yet."""

LOG_TAIL_LINES = 50

_FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
_FAILED_STATES = frozenset({"failure", "error"})

_GITHUB_REPO_URL = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?"
)

_MARK_READY = """
mutation($id: ID!) {
  markPullRequestReadyForReview(input: {pullRequestId: $id}) {
    pullRequest { isDraft }
  }
}
"""


@dataclass(frozen=True)
class PullRequest:
    repo: Repo
    number: int
    url: str
    """The pull request's page on GitHub."""


class _Head(BaseModel):
    sha: str


class _PullRequest(BaseModel):
    number: int
    html_url: str
    node_id: str
    state: Literal["open", "closed"]
    draft: bool
    merged_at: datetime | None
    head: _Head


class _App(BaseModel):
    slug: str


class _CheckOutput(BaseModel):
    title: str | None
    summary: str | None


class _CheckRun(BaseModel):
    id: int
    name: str
    status: str
    conclusion: str | None
    details_url: str | None
    output: _CheckOutput
    app: _App | None


class _CheckRunPage(BaseModel):
    check_runs: list[_CheckRun]


class _WorkflowRun(BaseModel):
    id: int
    status: str | None


class _WorkflowRunPage(BaseModel):
    workflow_runs: list[_WorkflowRun]


class _Job(BaseModel):
    id: int
    name: str
    status: str
    conclusion: str | None
    html_url: str | None


class _JobPage(BaseModel):
    jobs: list[_Job]


class _Status(BaseModel):
    context: str
    state: str
    description: str | None
    target_url: str | None


class _CombinedStatusPage(BaseModel):
    statuses: list[_Status]


class _Review(BaseModel):
    user: api.User
    body: str | None
    state: str
    submitted_at: datetime | None


class _ReviewComment(BaseModel):
    user: api.User
    body: str
    path: str
    line: int | None
    original_line: int | None
    created_at: datetime


class _IssueComment(BaseModel):
    user: api.User
    body: str
    created_at: datetime


class _GraphQLError(BaseModel):
    message: str


class _GraphQLResponse(BaseModel):
    errors: list[_GraphQLError] = Field(default_factory=list)


_PULLS = TypeAdapter(list[_PullRequest])
_REVIEWS = TypeAdapter(list[_Review])
_REVIEW_COMMENTS = TypeAdapter(list[_ReviewComment])
_ISSUE_COMMENTS = TypeAdapter(list[_IssueComment])


def _github_repo(repo_url: str) -> tuple[str, str]:
    match = _GITHUB_REPO_URL.fullmatch(repo_url)
    if match is None:
        raise ValueError(
            f"{repo_url!r} is not a GitHub repository URL "
            "(expected https://github.com/<owner>/<repo>)"
        )
    return match["owner"], match["repo"]


def github_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=api.API_URL,
        headers=api.headers(token),
    )


def _get_pages(client: httpx.Client, path: str, params: dict[str, str]) -> list[bytes]:
    pages: list[bytes] = []
    response = client.get(path, params={**params, "per_page": "100"})
    while True:
        response.raise_for_status()
        pages.append(response.content)
        next_url = response.links.get("next", {}).get("url")
        if next_url is None:
            return pages
        response = client.get(next_url)


def _open_prompt(repo: Repo, ticket_url: str, *, draft: bool) -> str:
    kind = "draft pull request" if draft else "pull request"
    flag = " --draft" if draft else ""
    return f"""\
Publish your work as a {kind}:

1. Commit any uncommitted work on the branch {repo.branch}. Stay on that branch.
2. Push {repo.branch} to origin.
3. Open a {kind} from {repo.branch} into {repo.base} with
   `gh pr create --head {repo.branch} --base {repo.base}{flag}`. Give it a
   title and a description of the change, and reference the ticket it
   resolves: {ticket_url}

git and gh are already authenticated. Afterwards Fledge checks that the working
tree is clean, that {repo.branch} on origin is your local HEAD, and that the
pull request is open.
"""


def _push_prompt(repo: Repo) -> str:
    return f"""\
Push your work on the branch {repo.branch}:

1. Fetch {repo.branch} from origin. If it has commits you do not have (a person
   may have pushed to it), merge them in and resolve any conflicts.
2. Commit any uncommitted work.
3. Push {repo.branch} to origin.

git and gh are already authenticated. Afterwards Fledge checks that the working
tree is clean and that {repo.branch} on origin is your local HEAD.
"""


def check_pushed(repo: Repo, token: str) -> None:
    """Raise RuntimeError unless the work is committed and on origin."""
    status = git(repo.path, "status", "--porcelain")
    if status:
        raise RuntimeError(
            f"the working tree of {repo.path} has uncommitted changes:\n{status}"
        )
    local = git(repo.path, "rev-parse", "HEAD")
    remote = git(
        repo.path, "ls-remote", "origin", f"refs/heads/{repo.branch}", token=token
    )
    if not remote:
        raise RuntimeError(f"branch {repo.branch} does not exist on origin")
    remote_sha = remote.split()[0]
    if remote_sha != local:
        raise RuntimeError(
            f"{repo.branch} on origin is at {remote_sha}, "
            f"but the local HEAD is at {local}"
        )


def find_pull_request(client: httpx.Client, repo: Repo) -> PullRequest:
    """The one open pull request from the repository's working branch."""
    owner, name = _github_repo(repo.url)
    head = f"{owner}:{repo.branch}"
    pages = _get_pages(
        client, f"/repos/{owner}/{name}/pulls", {"head": head, "state": "open"}
    )
    found = [pull for page in pages for pull in _PULLS.validate_json(page)]
    if len(found) != 1:
        raise RuntimeError(
            f"expected one open pull request from {head}, found {len(found)}"
        )
    return PullRequest(repo, found[0].number, found[0].html_url)


@step
def verify_opened(ctx: StepContext, repo: Repo) -> PullRequest:
    token = api.token()
    check_pushed(repo, token)
    with github_client(token) as client:
        pull = find_pull_request(client, repo)
    ctx.log(f"verified {repo.branch} is pushed and {pull.url} is open")
    return pull


@step
def verify_pushed(ctx: StepContext, repo: Repo) -> None:
    check_pushed(repo, api.token())
    ctx.log(f"verified {repo.branch} is pushed")


def open_pull_request(agent: Agent, repo: Repo, *, draft: bool = False) -> PullRequest:
    """Have the agent push its branch and open a pull request, then find it."""
    ticket_url = engine.current_run().work.url
    agent.prompt(_open_prompt(repo, ticket_url, draft=draft), repo)
    return verify_opened(repo)


def push(agent: Agent, pr: PullRequest) -> None:
    """Have the agent merge in the remote branch and push, then verify it did."""
    agent.prompt(_push_prompt(pr.repo), pr.repo)
    verify_pushed(pr.repo)


def _pull_request(client: httpx.Client, pr: PullRequest) -> _PullRequest:
    owner, name = _github_repo(pr.repo.url)
    response = client.get(f"/repos/{owner}/{name}/pulls/{pr.number}")
    response.raise_for_status()
    return _PullRequest.model_validate_json(response.content)


def ready_for_review(client: httpx.Client, pr: PullRequest) -> None:
    state = _pull_request(client, pr)
    if not state.draft:
        return
    # The REST API cannot take a pull request out of draft.
    response = client.post(
        "/graphql", json={"query": _MARK_READY, "variables": {"id": state.node_id}}
    )
    response.raise_for_status()
    errors = _GraphQLResponse.model_validate_json(response.content).errors
    if errors:
        raise RuntimeError(
            f"GitHub did not mark {pr.url} ready for review: "
            + "; ".join(error.message for error in errors)
        )


@step
def mark_ready(ctx: StepContext, pr: PullRequest) -> None:
    with github_client(api.token()) as client:
        ready_for_review(client, pr)
    ctx.log(f"marked {pr.url} ready for review")


@dataclass(frozen=True)
class _Ci:
    failures: list[FeedbackItem]
    pending: bool
    reported: bool
    """Whether the commit has any check run or status at all."""


def _job_log_tail(client: httpx.Client, pr: PullRequest, job_id: int) -> str:
    owner, name = _github_repo(pr.repo.url)
    # Answered with a redirect to a short-lived download URL.
    response = client.get(
        f"/repos/{owner}/{name}/actions/jobs/{job_id}/logs", follow_redirects=True
    )
    if response.status_code in (httpx.codes.NOT_FOUND, httpx.codes.GONE):
        return "The job's log is no longer available."
    response.raise_for_status()
    tail = "\n".join(response.text.splitlines()[-LOG_TAIL_LINES:])
    return f"End of the job's log:\n{tail}"


def _check_run_feedback(
    client: httpx.Client, pr: PullRequest, run: _CheckRun
) -> FeedbackItem:
    parts = [f"{run.name} concluded {run.conclusion}."]
    parts += [text for text in (run.output.title, run.output.summary) if text]
    if run.details_url:
        parts.append(f"Details: {run.details_url}")
    if run.app is not None and run.app.slug == "github-actions":
        parts.append(_job_log_tail(client, pr, run.id))
    return FeedbackItem(run.name, "\n\n".join(parts))


def _status_feedback(status: _Status) -> FeedbackItem:
    parts = [f"{status.context} reported {status.state}."]
    if status.description:
        parts.append(status.description)
    if status.target_url:
        parts.append(f"Details: {status.target_url}")
    return FeedbackItem(status.context, "\n\n".join(parts))


def _actions_check_runs(
    client: httpx.Client, repo: str, sha: str
) -> tuple[list[_CheckRun], bool]:
    """The commit's GitHub Actions jobs as check runs, and whether a workflow
    run is still going (it may not have created all its jobs yet)."""
    workflow_runs = [
        run
        for page in _get_pages(client, f"{repo}/actions/runs", {"head_sha": sha})
        for run in _WorkflowRunPage.model_validate_json(page).workflow_runs
    ]
    jobs = [
        job
        for run in workflow_runs
        for page in _get_pages(client, f"{repo}/actions/runs/{run.id}/jobs", {})
        for job in _JobPage.model_validate_json(page).jobs
    ]
    # An Actions job and its check run share an id, so the log lookup works.
    check_runs = [
        _CheckRun(
            id=job.id,
            name=job.name,
            status=job.status,
            conclusion=job.conclusion,
            details_url=job.html_url,
            output=_CheckOutput(title=None, summary=None),
            app=_App(slug="github-actions"),
        )
        for job in jobs
    ]
    return check_runs, any(run.status != "completed" for run in workflow_runs)


def _check_runs(
    client: httpx.Client, repo: str, sha: str
) -> tuple[list[_CheckRun], bool]:
    """The commit's check runs, and whether more are known to be coming."""
    try:
        pages = _get_pages(client, f"{repo}/commits/{sha}/check-runs", {})
    except httpx.HTTPStatusError as err:
        if err.response.status_code != httpx.codes.FORBIDDEN:
            raise
        # GitHub refuses check runs to fine-grained personal access tokens,
        # which cannot be given the Checks permission. Such a token can still
        # read GitHub Actions, which covers the checks most repositories
        # have; checks reported by other apps stay invisible to it.
        logger.warning(
            "GITHUB_TOKEN cannot read check runs; reading GitHub Actions jobs instead"
        )
        return _actions_check_runs(client, repo, sha)
    runs = [
        run
        for page in pages
        for run in _CheckRunPage.model_validate_json(page).check_runs
    ]
    return runs, False


def _ci(client: httpx.Client, pr: PullRequest, sha: str) -> _Ci:
    owner, name = _github_repo(pr.repo.url)
    commit = f"/repos/{owner}/{name}/commits/{sha}"
    runs, more_coming = _check_runs(client, f"/repos/{owner}/{name}", sha)
    statuses = [
        status
        for page in _get_pages(client, f"{commit}/status", {})
        for status in _CombinedStatusPage.model_validate_json(page).statuses
    ]
    failures = [
        _check_run_feedback(client, pr, run)
        for run in runs
        if run.status == "completed" and run.conclusion in _FAILED_CONCLUSIONS
    ]
    failures += [_status_feedback(s) for s in statuses if s.state in _FAILED_STATES]
    pending = (
        more_coming
        or any(run.status != "completed" for run in runs)
        or any(status.state == "pending" for status in statuses)
    )
    return _Ci(failures, pending, bool(runs or statuses or more_coming))


def ci_verdict(
    client: httpx.Client, pr: PullRequest, started_at: datetime, now: datetime
) -> Verdict:
    """What `checks_green` reports, with the clock passed in."""
    state = _pull_request(client, pr)
    if state.merged_at is not None:
        return Passed()
    if state.state == "closed":
        return Stopped()
    ci = _ci(client, pr, state.head.sha)
    if ci.failures:
        return Feedback(ci.failures)
    if ci.pending:
        return Pending()
    # Counted from when the wait began, not from the loop's last feedback:
    # the agent's fix may take longer than the grace period, and its push
    # must still get the whole period for CI to register.
    if not ci.reported and now < started_at + NO_CHECKS_GRACE:
        return Pending()
    return Passed()


def _review_feedback(
    client: httpx.Client, pr: PullRequest, since: datetime
) -> list[tuple[datetime, api.User, str]]:
    owner, name = _github_repo(pr.repo.url)
    pull = f"/repos/{owner}/{name}/pulls/{pr.number}"
    found: list[tuple[datetime, api.User, str]] = []
    for page in _get_pages(client, f"{pull}/reviews", {}):
        for review in _REVIEWS.validate_json(page):
            body = (review.body or "").strip()
            if review.submitted_at is None or review.submitted_at <= since:
                continue
            if review.state == "CHANGES_REQUESTED":
                found.append(
                    (review.submitted_at, review.user, body or "Changes requested.")
                )
            elif review.state in {"COMMENTED", "APPROVED"} and body:
                found.append((review.submitted_at, review.user, body))
    # GitHub's `since` filters on updated_at; created_at is checked below so
    # edits to old comments are not delivered again.
    for page in _get_pages(client, f"{pull}/comments", {"since": since.isoformat()}):
        for comment in _REVIEW_COMMENTS.validate_json(page):
            if comment.created_at <= since:
                continue
            line = comment.line if comment.line is not None else comment.original_line
            where = comment.path if line is None else f"{comment.path} line {line}"
            found.append(
                (comment.created_at, comment.user, f"On {where}:\n{comment.body}")
            )
    issue_comments = f"/repos/{owner}/{name}/issues/{pr.number}/comments"
    for page in _get_pages(client, issue_comments, {"since": since.isoformat()}):
        found.extend(
            (comment.created_at, comment.user, comment.body)
            for comment in _ISSUE_COMMENTS.validate_json(page)
            if comment.created_at > since
        )
    return found


def review_verdict(client: httpx.Client, pr: PullRequest, since: datetime) -> Verdict:
    """What `merged` reports."""
    state = _pull_request(client, pr)
    if state.merged_at is not None:
        return Passed()
    if state.state == "closed":
        return Stopped()
    response = client.get("/user")
    response.raise_for_status()
    own_login = api.User.model_validate_json(response.content).login
    human = sorted(
        (
            (at, user.login, body)
            for at, user, body in _review_feedback(client, pr, since)
            if user.type != "Bot" and user.login != own_login
        ),
        key=lambda found: found[0],
    )
    items = [FeedbackItem(login, body) for _, login, body in human]
    items += _ci(client, pr, state.head.sha).failures
    return Feedback(items) if items else Pending()


@wait
def checks_green(ctx: WaitContext, pr: PullRequest) -> Verdict:
    """Passes when the pull request's checks pass; feedback when one fails."""
    # The one place a check reads the real clock: waits may, workflow bodies may not.
    with github_client(api.token()) as client:
        return ci_verdict(client, pr, ctx.started_at, datetime.now(UTC))


@wait
def merged(ctx: WaitContext, pr: PullRequest) -> Verdict:
    """Passes when the pull request is merged; feedback for reviews and failures."""
    with github_client(api.token()) as client:
        return review_verdict(client, pr, ctx.since)
