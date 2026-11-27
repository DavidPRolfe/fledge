"""What the GitHub trigger and the pull request calls share to reach the API."""

import os
import re
from datetime import datetime

import httpx
from pydantic import BaseModel

from fledge.git import GITHUB_TOKEN_ENV

API_URL = "https://api.github.com"
GIT_URL = "https://github.com"


class MissingTokenError(RuntimeError):
    pass


class User(BaseModel):
    login: str
    type: str


class IssueComment(BaseModel):
    """A comment on an issue or in a pull request's conversation."""

    id: int
    body: str
    user: User
    created_at: datetime


_REPO = re.compile(r"[\w.-]+/[\w.-]+")


def check_repo(repo: str) -> None:
    if not _REPO.fullmatch(repo):
        raise ValueError(f"repo must be 'owner/name', got {repo!r}")


def token() -> str:
    value = os.environ.get(GITHUB_TOKEN_ENV)
    if not value:
        raise MissingTokenError(f"{GITHUB_TOKEN_ENV} is not set")
    return value


def headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def client(token: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """`transport` exists so tests can replace the network."""
    return httpx.Client(base_url=API_URL, headers=headers(token), transport=transport)


def get_pages(client: httpx.Client, path: str, params: dict[str, str]) -> list[bytes]:
    """The body of every page of a paginated listing."""
    pages: list[bytes] = []
    response = client.get(path, params={**params, "per_page": "100"})
    while True:
        response.raise_for_status()
        pages.append(response.content)
        next_url = response.links.get("next", {}).get("url")
        if next_url is None:
            return pages
        response = client.get(next_url)
