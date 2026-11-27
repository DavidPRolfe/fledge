"""Trigger for open GitHub issues in one repository that carry a label."""

from dataclasses import dataclass
from datetime import datetime

import httpx
from pydantic import BaseModel, TypeAdapter

from fledge.core import WorkItem
from fledge.github import api
from fledge.triggers import Comment, TriggerCheckError


class _PullRequestLink(BaseModel):
    pass


class _Issue(BaseModel):
    number: int
    title: str
    body: str | None
    html_url: str
    pull_request: _PullRequestLink | None = None


_ISSUES = TypeAdapter(list[_Issue])
_COMMENTS = TypeAdapter(list[api.IssueComment])


class GitHubIssuesTrigger:
    """Open issues in `repo` ("owner/name") carrying `label`.

    The token is read from GITHUB_TOKEN when the trigger is constructed; if it
    is unset or empty, construction raises MissingTokenError rather than
    falling back to unauthenticated requests, whose low rate limit and lack of
    private-repo access would surface later as confusing failures.

    `transport` exists so tests can replace the network; workflow code never
    passes it.
    """

    def __init__(
        self,
        repo: str,
        label: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = api.token()
        self._repo = repo
        self._label = label
        self._transport = transport

    def _client(self) -> httpx.Client:
        return api.client(self._token, self._transport)

    def _work_item(self, issue: _Issue) -> WorkItem:
        return WorkItem(
            external_id=str(issue.number),
            title=issue.title,
            body=issue.body or "",
            url=issue.html_url,
            repo_url=f"https://github.com/{self._repo}.git",
        )

    def check(self) -> None:
        """Check GITHUB_TOKEN can read the repository and its issues and push.

        Pushing is checked because agents push and open pull requests with the
        same token; without it a run fails only after the agent has finished.
        Permission to open pull requests cannot be checked without opening one.
        """
        with self._client() as client:
            repo = client.get(f"/repos/{self._repo}")
            issues = client.get(f"/repos/{self._repo}/issues", params={"per_page": "1"})
        self._raise_for_access(repo, "see")
        self._raise_for_access(issues, "read the issues of")
        with httpx.Client(
            base_url=api.GIT_URL,
            auth=httpx.BasicAuth("x-access-token", self._token),
            transport=self._transport,
        ) as client:
            # The ref advertisement git fetches before a push; GitHub answers
            # it only for credentials that may push.
            push = client.get(
                f"/{self._repo}.git/info/refs",
                params={"service": "git-receive-pack"},
            )
        self._raise_for_access(push, "push to")

    def _raise_for_access(self, response: httpx.Response, action: str) -> None:
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise TriggerCheckError(
                "GitHub rejected GITHUB_TOKEN; it may be expired or revoked"
            )
        # GitHub answers 404 rather than 403 for private repositories the
        # token cannot see.
        if response.status_code in (httpx.codes.FORBIDDEN, httpx.codes.NOT_FOUND):
            raise TriggerCheckError(
                f"GITHUB_TOKEN cannot {action} {self._repo} "
                f"(HTTP {response.status_code}). It needs Contents read/write, "
                "Issues read, Pull requests read/write and, to watch CI, Checks, "
                "Commit statuses and Actions read on the repository."
            )
        response.raise_for_status()

    def poll(self) -> list[WorkItem]:
        with self._client() as client:
            pages = api.get_pages(
                client,
                f"/repos/{self._repo}/issues",
                {"state": "open", "labels": self._label},
            )
        return [
            self._work_item(issue)
            for page in pages
            for issue in _ISSUES.validate_json(page)
            if issue.pull_request is None
        ]

    def fetch(self, ref: str) -> WorkItem:
        """The issue numbered `ref`, whatever its state or labels.

        Raises ValueError if `ref` is not a plain issue number or names a pull
        request, and httpx.HTTPStatusError if GitHub has no such issue.
        """
        if not (ref.isascii() and ref.isdigit()):
            raise ValueError(f"GitHub issue ref must be an issue number, got {ref!r}")
        with self._client() as client:
            response = client.get(f"/repos/{self._repo}/issues/{int(ref)}")
        response.raise_for_status()
        issue = _Issue.model_validate_json(response.content)
        if issue.pull_request is not None:
            raise ValueError(f"{self._repo}#{ref} is a pull request, not an issue")
        return self._work_item(issue)

    def poll_guidance(self, item_external_id: str, since: datetime) -> list[Comment]:
        """Comments created after `since` by accounts that are not bots.

        `since` must be timezone-aware: GitHub timestamps are UTC.
        """
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        with self._client() as client:
            # GitHub's `since` filters on updated_at, which still narrows the
            # transfer; created_at is checked below so edits to old comments
            # are not delivered again.
            pages = api.get_pages(
                client,
                f"/repos/{self._repo}/issues/{int(item_external_id)}/comments",
                {"since": since.isoformat()},
            )
        return [
            Comment(
                external_id=str(comment.id),
                author=comment.user.login,
                body=comment.body,
            )
            for page in pages
            for comment in _COMMENTS.validate_json(page)
            if comment.created_at > since and comment.user.type != "Bot"
        ]


@dataclass(frozen=True)
class GitHubIssues:
    """Declares a GitHubIssuesTrigger in workflow code; see TriggerSpec."""

    repo: str
    label: str

    def __post_init__(self) -> None:
        api.check_repo(self.repo)
        # GitHub takes the labels filter as a comma-separated list, so a comma
        # would silently turn one label into several.
        if not self.label or "," in self.label:
            raise ValueError(
                f"label must be non-empty without commas, got {self.label!r}"
            )

    def start(self) -> GitHubIssuesTrigger:
        return GitHubIssuesTrigger(self.repo, self.label)
