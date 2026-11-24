import os
from dataclasses import dataclass

from fledge.builtin.steps.git import GITHUB_TOKEN_ENV, git
from fledge.core import StepContext


@dataclass(frozen=True)
class CloneRepo:
    """Clone the work item's repository into the empty workspace.

    Uses GITHUB_TOKEN when it is set; without it the clone is anonymous, which
    works for public repositories and fails for private ones.
    """

    name: str = "clone"

    def run(self, ctx: StepContext) -> dict[str, str]:
        git(
            ctx.workspace,
            "clone",
            "--",
            ctx.work.repo_url,
            str(ctx.workspace),
            token=os.environ.get(GITHUB_TOKEN_ENV),
        )
        ctx.log(f"cloned {ctx.work.repo_url}")
        return {}
