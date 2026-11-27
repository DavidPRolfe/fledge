"""Writes the starter workflow for `fledge init`."""

import json
from pathlib import Path

from fledge.github import GitHubIssues

# Braces the written file's own IMPLEMENT template needs are doubled here.
_GITHUB_WORKFLOW = '''\
"""The workflow runs once per labelled issue. An agent implements the issue,
opens a pull request, and handles CI failures and review comments until it is
merged.

Each step runs in its own container. To resume a run, Fledge calls this
function again from the top and reuses the results of steps that already ran,
so keep I/O and clock reads inside steps. Do not catch BaseException: Fledge
pauses a run by raising one.
"""

from fledge import Agent, Run, until, workflow
from fledge.git import clone_repo
from fledge.github import (
    GitHubIssues,
    checks_green,
    mark_ready,
    merged,
    open_pull_request,
    push,
)
from fledge.harnesses import ClaudeCode

IMPLEMENT = """\\
Resolve this ticket by changing the repository in the current working directory.

Title: {{work.title}}
Link: {{work.url}}

{{work.body}}

Make the change, run the project's checks if it has any, and commit your work
on the current branch. Do not push and do not open a pull request yet: you
will be asked to. End with a short summary of what you changed and why.
"""

implementer = Agent("implementer", ClaudeCode())


@workflow(trigger=GitHubIssues(repo={repo}, label={label}))
def {name}(run: Run) -> None:
    repo = clone_repo(run.work.repo_url)
    implementer.prompt(IMPLEMENT.format(work=run.work), repo)

    # If CI is still failing after 5 fixes, the run fails.
    pr = open_pull_request(implementer, repo, draft=True)
    for feedback in until(checks_green(pr), max_rounds=5):
        implementer.prompt(feedback, repo)
        push(implementer, pr)
    mark_ready(pr)

    # If the pull request is closed without merging, the run is cancelled.
    for feedback in until(merged(pr), max_rounds=20):
        implementer.prompt(feedback, repo)
        push(implementer, pr)
'''


def _python_str(value: str) -> str:
    # A JSON string without ASCII escaping is also a valid Python literal for
    # the same text, and uses the double quotes the formatter would.
    return json.dumps(value, ensure_ascii=False)


def write_github_workflow(directory: Path, name: str, repo: str, label: str) -> Path:
    """Write a workflow that turns labelled issues in `repo` into pull requests.

    Refuses to overwrite an existing file.
    """
    if not name.isidentifier():
        raise ValueError(f"name must be a Python identifier, got {name!r}")
    # Raises ValueError for a repo or label the written trigger would refuse.
    GitHubIssues(repo, label)
    source = _GITHUB_WORKFLOW.format(
        name=name,
        repo=_python_str(repo),
        label=_python_str(label),
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    with path.open("x") as file:
        file.write(source)
    return path
