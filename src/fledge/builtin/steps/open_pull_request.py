import os
import re
from dataclasses import dataclass, field

import httpx

from fledge.builtin.steps.git import GITHUB_TOKEN_ENV, git
from fledge.core import StepContext

GITHUB_API_URL = "https://api.github.com"
COMMIT_AUTHOR_NAME = "fledge"
COMMIT_AUTHOR_EMAIL = "fledge@users.noreply.github.com"

_GITHUB_REPO_URL = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?"
)


def _github_repo(repo_url: str) -> tuple[str, str]:
    match = _GITHUB_REPO_URL.fullmatch(repo_url)
    if match is None:
        raise ValueError(
            f"{repo_url!r} is not a GitHub repository URL "
            "(expected https://github.com/<owner>/<repo>)"
        )
    return match["owner"], match["repo"]


@dataclass(frozen=True)
class OpenPullRequest:
    """Commit the workspace's changes on a new branch, push it, open a PR.

    GitHub only, and GITHUB_TOKEN is required.
    """

    name: str = "open_pr"
    # Tests pass an httpx.MockTransport; None means httpx's normal transport.
    transport: httpx.BaseTransport | None = field(
        default=None, repr=False, compare=False
    )

    def run(self, ctx: StepContext) -> dict[str, str]:
        owner, repo = _github_repo(ctx.work.repo_url)
        token = os.environ.get(GITHUB_TOKEN_ENV)
        if token is None:
            raise RuntimeError(f"{GITHUB_TOKEN_ENV} must be set to push and open a PR")
        workspace = ctx.workspace
        branch = f"fledge/{ctx.run_id.hex[:8]}"
        base = git(
            workspace, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
        ).removeprefix("origin/")

        git(workspace, "switch", "--create", branch)
        if git(workspace, "status", "--porcelain"):
            git(workspace, "add", "--all")
            git(
                workspace,
                "-c",
                f"user.name={COMMIT_AUTHOR_NAME}",
                "-c",
                f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit",
                "--message",
                ctx.work.title,
            )
        if git(workspace, "rev-list", "--count", f"origin/{base}..HEAD") == "0":
            raise RuntimeError(
                f"no changes to propose: {branch} has no commits beyond {base}"
            )
        head_sha = git(workspace, "rev-parse", "HEAD")
        git(workspace, "push", "origin", f"HEAD:refs/heads/{branch}", token=token)
        ctx.log(f"pushed {branch} at {head_sha}")

        with httpx.Client(
            base_url=GITHUB_API_URL,
            transport=self.transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as client:
            response = client.post(
                f"/repos/{owner}/{repo}/pulls",
                json={
                    "title": ctx.work.title,
                    "head": branch,
                    "base": base,
                    "body": _pull_request_body(ctx),
                },
            )
            response.raise_for_status()
        pull_request_url: str = response.json()["html_url"]
        ctx.log(f"opened {pull_request_url}")
        return {"pull_request_url": pull_request_url, "head_sha": head_sha}


def _pull_request_body(ctx: StepContext) -> str:
    body = f"Resolves {ctx.work.url}"
    summaries = [out["summary"] for out in ctx.outputs.values() if "summary" in out]
    if summaries:
        body += f"\n\n{summaries[-1]}"
    return body
