"""Trigger for open GitHub issues in one repository that carry a label."""

import os
from datetime import datetime

import httpx
from pydantic import BaseModel, TypeAdapter

from fledge.core import Comment, WorkItem

API_URL = "https://api.github.com"


class MissingTokenError(RuntimeError):
    pass


class _User(BaseModel):
    login: str
    type: str


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
    user: _User
    created_at: datetime


_ISSUES = TypeAdapter(list[_Issue])
_COMMENTS = TypeAdapter(list[_IssueComment])


class GitHubIssuesTrigger:
    """Open issues in `repo` ("owner/name") carrying `label`.

    The token is read from GITHUB_TOKEN when the trigger is constructed; if it
    is unset or empty, construction raises MissingTokenError rather than
    falling back to unauthenticated requests, whose low rate limit and lack of
    private-repo access would surface later as confusing failures.

    `transport` exists so tests can replace the network; the registry never
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
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise MissingTokenError("GITHUB_TOKEN is not set")
        self._repo = repo
        self._label = label
        self._token = token
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
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
