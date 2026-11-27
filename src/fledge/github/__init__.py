"""GitHub: the issues trigger and pull requests."""

from fledge.github.issues import GitHubIssues
from fledge.github.pulls import (
    PullRequest,
    checks_green,
    mark_ready,
    merged,
    open_pull_request,
    push,
)

__all__ = [
    "GitHubIssues",
    "PullRequest",
    "checks_green",
    "mark_ready",
    "merged",
    "open_pull_request",
    "push",
]
