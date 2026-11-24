"""The built-in workflows."""

from fledge.builtin.steps import AgentStep, CloneRepo, OpenPullRequest
from fledge.core import Workflow

IMPLEMENT = """\
Resolve this ticket by changing the repository in the current working directory.

Title: {title}
Link: {url}

{body}

Make the change, and run the project's checks if it has any. You may commit as
you go or leave changes uncommitted; anything uncommitted is committed for you
afterwards. Do not push and do not open a pull request: that happens after you
finish. End with a short summary of what you changed and why; it becomes the
pull request description.
"""

ticket_to_pr = Workflow(
    name="ticket_to_pr",
    steps=[
        CloneRepo(),
        AgentStep(name="implement", harness="claude_code", prompt=IMPLEMENT),
        OpenPullRequest(),
    ],
)
