"""Running git in the run workspace, shared by the built-in steps."""

import base64
import os
import subprocess
from pathlib import Path

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"  # noqa: S105 - name of an env var, not a secret


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


def git(workspace: Path, *args: str, token: str | None = None) -> str:
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
