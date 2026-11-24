"""Looks up extensions registered under the fledge entry point groups."""

from importlib.metadata import entry_points

from fledge.config import TriggerConfig
from fledge.core import Harness, Trigger, Workflow


def _load(group: str, name: str) -> object:
    matches = entry_points(group=group, name=name)
    if not matches:
        raise LookupError(f"no {group} entry point named {name!r}")
    loaded: object = matches[name].load()
    return loaded


def load_workflow(name: str) -> Workflow:
    workflow = _load("fledge.workflows", name)
    if not isinstance(workflow, Workflow):
        raise TypeError(f"fledge.workflows entry point {name!r} is not a Workflow")
    return workflow


def load_trigger(config: TriggerConfig) -> Trigger:
    factory = _load("fledge.triggers", config.type)
    if not callable(factory):
        raise TypeError(f"fledge.triggers entry point {config.type!r} is not callable")
    trigger = factory(**config.options)
    if not isinstance(trigger, Trigger):
        raise TypeError(f"fledge.triggers entry point {config.type!r} is not a Trigger")
    return trigger


def load_harness(name: str) -> Harness:
    factory = _load("fledge.harnesses", name)
    if not callable(factory):
        raise TypeError(f"fledge.harnesses entry point {name!r} is not callable")
    harness = factory()
    if not isinstance(harness, Harness):
        raise TypeError(f"fledge.harnesses entry point {name!r} is not a Harness")
    return harness
