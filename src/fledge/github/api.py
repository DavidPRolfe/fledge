"""What the GitHub trigger and the pull request calls share to reach the API."""

import os

from pydantic import BaseModel

from fledge.git import GITHUB_TOKEN_ENV

API_URL = "https://api.github.com"
GIT_URL = "https://github.com"


class MissingTokenError(RuntimeError):
    pass


class User(BaseModel):
    login: str
    type: str


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
