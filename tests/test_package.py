import subprocess
import sys

import fledge

_LOADED_CONTROL_MODULES = """\
import sys

import fledge, fledge.executor, fledge.github, fledge.harnesses, fledge.steps

print(*sorted(name for name in sys.modules if name.startswith("fledge.control")))
"""


def test_package_has_docstring() -> None:
    assert fledge.__doc__ == "fledge: open source software factory."


def test_workflow_code_and_the_step_executor_do_not_import_the_control_plane() -> None:
    result = subprocess.run(  # noqa: S603 - runs this interpreter on a fixed script
        [sys.executable, "-c", _LOADED_CONTROL_MODULES],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.split() == []
