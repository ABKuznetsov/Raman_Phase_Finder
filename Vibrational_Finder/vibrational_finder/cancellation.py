"""Cooperative cancellation for the current background job."""

from contextvars import ContextVar
from threading import Event


class JobCancelled(Exception):
    """The job was superseded or cancelled; no result should be applied."""


current_cancel_event: ContextVar[Event | None] = ContextVar("current_cancel_event", default=None)


def checkpoint() -> None:
    event = current_cancel_event.get()
    if event is not None and event.is_set():
        raise JobCancelled()
