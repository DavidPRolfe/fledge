"""Built-in steps."""

import os
import re

from fledge.core import Repo, StepContext
from fledge.engine import step
from fledge.git import GITHUB_TOKEN_ENV, git

__all__ = ["clone_repo"]


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
