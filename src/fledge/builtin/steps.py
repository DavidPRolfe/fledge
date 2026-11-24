"""The built-in steps: clone a repository, run an agent, open a pull request."""

import base64
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from fledge.core import StepContext

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"  # noqa: S105 - name of an env var, not a secret
GITHUB_API_URL = "https://api.github.com"
COMMIT_AUTHOR_NAME = "fledge"
COMMIT_AUTHOR_EMAIL = "fledge@users.noreply.github.com"

_GITHUB_REPO_URL = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?"
)


class GitError(RuntimeError):
    pass


def _git_env(token: str | None) -> dict[str, str]:
    # The workspace is shared with the agent step, so the token must not land in
    # .git/config or the remote URL. It is handed to this one git process as an
    # http.extraHeader through GIT_CONFIG_* environment variables, which git
    # reads as command-line config: not persisted, and not visible in the
    # process's argument list. The header is scoped to github.com so a remote
    # pointing anywhere else never receives it.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token is not None:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
    return env


def _git(workspace: Path, *args: str, token: str | None = None) -> str:
    result = subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", *args],  # noqa: S607 - git is resolved from PATH in the step image
        cwd=workspace,
        env=_git_env(token),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(
            f"git {args[0]} failed with exit code {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _github_repo(repo_url: str) -> tuple[str, str]:
    match = _GITHUB_REPO_URL.fullmatch(repo_url)
    if match is None:
        raise ValueError(
            f"{repo_url!r} is not a GitHub repository URL "
            "(expected https://github.com/<owner>/<repo>)"
        )
    return match["owner"], match["repo"]


@dataclass(frozen=True)
class CloneRepo:
    """Clone the work item's repository into the empty workspace.

    Uses GITHUB_TOKEN when it is set; without it the clone is anonymous, which
    works for public repositories and fails for private ones.
    """

    name: str = "clone"

    def run(self, ctx: StepContext) -> dict[str, str]:
        _git(
            ctx.workspace,
            "clone",
            "--",
            ctx.work.repo_url,
            str(ctx.workspace),
            token=os.environ.get(GITHUB_TOKEN_ENV),
        )
        ctx.log(f"cloned {ctx.work.repo_url}")
        return {}


@dataclass(frozen=True)
class AgentStep:
    """Run agent turns until the agent yields with no guidance waiting.

    `prompt` is a str.format template over `title`, `body` and `url`.
    """

    name: str
    harness: str
    prompt: str

    def run(self, ctx: StepContext) -> dict[str, str]:
        agent = ctx.harness(self.harness)
        prompt = self.prompt.format(
            title=ctx.work.title, body=ctx.work.body, url=ctx.work.url
        )
        session_id: str | None = None
        while True:
            turn = agent.run_turn(ctx.workspace, prompt, session_id, ctx.agent_event)
            session_id = turn.session_id
            guidance = ctx.claim_guidance()
            if not guidance:
                return {"summary": turn.final_message}
            ctx.log(f"delivering {len(guidance)} guidance message(s) to the agent")
            prompt = _guidance_prompt(guidance)


def _guidance_prompt(bodies: list[str]) -> str:
    messages = "\n\n".join(
        f"Message {number}:\n{body}" for number, body in enumerate(bodies, start=1)
    )
    return (
        "A person overseeing this work sent the following guidance. "
        "Take it into account and continue.\n\n" + messages
    )


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
        base = _git(
            workspace, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
        ).removeprefix("origin/")

        _git(workspace, "switch", "--create", branch)
        if _git(workspace, "status", "--porcelain"):
            _git(workspace, "add", "--all")
            _git(
                workspace,
                "-c",
                f"user.name={COMMIT_AUTHOR_NAME}",
                "-c",
                f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit",
                "--message",
                ctx.work.title,
            )
        if _git(workspace, "rev-list", "--count", f"origin/{base}..HEAD") == "0":
            raise RuntimeError(
                f"no changes to propose: {branch} has no commits beyond {base}"
            )
        head_sha = _git(workspace, "rev-parse", "HEAD")
        _git(workspace, "push", "origin", f"HEAD:refs/heads/{branch}", token=token)
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
