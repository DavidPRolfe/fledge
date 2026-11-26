"""Looks up harnesses registered under the fledge.harnesses entry point group."""

from importlib.metadata import entry_points

from fledge.core import Harness


def _load(group: str, name: str) -> object:
    matches = entry_points(group=group, name=name)
    if not matches:
        raise LookupError(f"no {group} entry point named {name!r}")
    loaded: object = matches[name].load()
    return loaded


def load_harness(name: str) -> Harness:
    factory = _load("fledge.harnesses", name)
    if not callable(factory):
        raise TypeError(f"fledge.harnesses entry point {name!r} is not callable")
    harness = factory()
    if not isinstance(harness, Harness):
        raise TypeError(f"fledge.harnesses entry point {name!r} is not a Harness")
    return harness
