"""The interfaces a trigger implements."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from fledge.core import WorkItem


@dataclass(frozen=True)
class Comment:
    external_id: str
    author: str
    body: str


class TriggerCheckError(RuntimeError):
    """The trigger cannot do its job, e.g. its credentials lack a permission."""


@runtime_checkable
class Trigger(Protocol):
    async def check(self) -> None:
        """Raise TriggerCheckError if the trigger or its workflow cannot work.

        Called once at startup, before any polling.
        """
        ...

    async def poll(self) -> list[WorkItem]:
        """Work that currently qualifies. Returning an item again is harmless."""
        ...

    async def fetch(self, ref: str) -> WorkItem:
        """One specific item, for a manual launch."""
        ...

    async def poll_guidance(
        self, item_external_id: str, since: datetime
    ) -> list[Comment]:
        """Human comments on the item made after `since`."""
        ...


class TriggerSpec(Protocol):
    """A workflow's trigger as declared in workflow code.

    Declaring one must not do I/O or read secrets: workflow modules are also
    imported in step containers, which never start the trigger.
    """

    def start(self) -> Trigger:
        """Build the live trigger. Called once, in the control plane."""
        ...
