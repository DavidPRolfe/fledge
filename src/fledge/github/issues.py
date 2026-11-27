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


class _IssueComment(BaseModel):
    id: int
    body: str
    user: api.User
    created_at: datetime


_ISSUES = TypeAdapter(list[_Issue])
_COMMENTS = TypeAdapter(list[_IssueComment])


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
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name:
            raise ValueError(f"repo must be 'owner/name', got {repo!r}")
        # GitHub takes the labels filter as a comma-separated list, so a comma
        # would silently turn one label into several.
        if not label or "," in label:
            raise ValueError(f"label must be non-empty without commas, got {label!r}")
        self._token = api.token()
        self._repo = repo
        self._label = label
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=api.API_URL,
            headers=api.headers(self._token),
            transport=self._transport,
        )

    def _work_item(self, issue: _Issue) -> WorkItem:
        return WorkItem(
            external_id=str(issue.number),
            title=issue.title,
            body=issue.body or "",
            url=issue.html_url,
            repo_url=f"https://github.com/{self._repo}.git",
        )

    async def _get_pages(
        self, client: httpx.AsyncClient, path: str, params: dict[str, str]
    ) -> list[bytes]:
        pages: list[bytes] = []
        response = await client.get(path, params={**params, "per_page": "100"})
        while True:
            response.raise_for_status()
            pages.append(response.content)
            next_url = response.links.get("next", {}).get("url")
            if next_url is None:
                return pages
            response = await client.get(next_url)

    async def check(self) -> None:
        """Check GITHUB_TOKEN can read the repository and its issues and push.

        Pushing is checked because agents push and open pull requests with the
        same token; without it a run fails only after the agent has finished.
        Permission to open pull requests cannot be checked without opening one.
        """
        async with self._client() as client:
            repo = await client.get(f"/repos/{self._repo}")
            issues = await client.get(
                f"/repos/{self._repo}/issues", params={"per_page": "1"}
            )
        self._raise_for_access(repo, "see")
        self._raise_for_access(issues, "read the issues of")
        async with httpx.AsyncClient(
            base_url=api.GIT_URL,
            auth=httpx.BasicAuth("x-access-token", self._token),
            transport=self._transport,
        ) as client:
            # The ref advertisement git fetches before a push; GitHub answers
            # it only for credentials that may push.
            push = await client.get(
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

    async def poll(self) -> list[WorkItem]:
        async with self._client() as client:
            pages = await self._get_pages(
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

    async def fetch(self, ref: str) -> WorkItem:
        """The issue numbered `ref`, whatever its state or labels.

        Raises ValueError if `ref` is not a plain issue number or names a pull
        request, and httpx.HTTPStatusError if GitHub has no such issue.
        """
        if not (ref.isascii() and ref.isdigit()):
            raise ValueError(f"GitHub issue ref must be an issue number, got {ref!r}")
        async with self._client() as client:
            response = await client.get(f"/repos/{self._repo}/issues/{int(ref)}")
        response.raise_for_status()
        issue = _Issue.model_validate_json(response.content)
        if issue.pull_request is not None:
            raise ValueError(f"{self._repo}#{ref} is a pull request, not an issue")
        return self._work_item(issue)

    async def poll_guidance(
        self, item_external_id: str, since: datetime
    ) -> list[Comment]:
        """Comments created after `since` by accounts that are not bots.

        `since` must be timezone-aware: GitHub timestamps are UTC.
        """
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        async with self._client() as client:
            # GitHub's `since` filters on updated_at, which still narrows the
            # transfer; created_at is checked below so edits to old comments
            # are not delivered again.
            pages = await self._get_pages(
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

    def start(self) -> GitHubIssuesTrigger:
        return GitHubIssuesTrigger(self.repo, self.label)
