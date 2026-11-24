import fledge


def test_package_has_docstring() -> None:
    assert fledge.__doc__ == "fledge: open source software factory."
