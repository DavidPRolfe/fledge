"""Built-in steps and checks."""

import os
import re
import shlex
import subprocess

from fledge.checks import Feedback, FeedbackItem, Passed, check
from fledge.core import Repo, StepContext
from fledge.engine import step
from fledge.git import GITHUB_TOKEN_ENV, git

__all__ = ["clone_repo", "command"]

OUTPUT_TAIL_LINES = 100


def _repository_name(url: str) -> str:
    name = re.split(r"[/:]", url.rstrip("/"))[-1].removesuffix(".git")
    if not name:
        raise ValueError(f"cannot tell the repository's name from {url!r}")
    return name


@step
def clone_repo(ctx: StepContext, url: str) -> Repo:
    """Clone into the workspace and check out the run's working branch.

    Uses GITHUB_TOKEN when it is set; without it the clone is anonymous, which
    works for public repositories and fails for private ones.
    """
    path = ctx.workspace / "repos" / _repository_name(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(
        path.parent,
        "clone",
        "--",
        url,
        str(path),
        token=os.environ.get(GITHUB_TOKEN_ENV),
    )
    base = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    base = base.removeprefix("origin/")
    branch = f"fledge/{ctx.run_id.hex[:8]}"
    git(path, "switch", "--create", branch)
    ctx.log(f"cloned {url} into {path} on {branch}, based on {base}")
    return Repo(url, path, branch, base)


@check
def command(ctx: StepContext, argv: list[str], repo: Repo) -> Passed | Feedback:
    """Passes when `argv` exits with status 0 in the repository."""
    ctx.log(f"running {shlex.join(argv)} in {repo.path}")
    result = subprocess.run(  # noqa: S603 - argv comes from workflow code and runs without a shell
        argv,
        cwd=repo.path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode == 0:
        return Passed()
    tail = "\n".join(result.stdout.splitlines()[-OUTPUT_TAIL_LINES:])
    body = (
        f"`{shlex.join(argv)}` exited with status {result.returncode}.\n\n"
        f"End of its output:\n{tail}"
    )
    return Feedback([FeedbackItem(argv[0], body)])
