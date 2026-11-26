from collections.abc import Callable
from contextvars import ContextVar


current_search_status: ContextVar[Callable[[str], None] | None] = ContextVar("current_search_status", default=None)


def report_search_status(message: str) -> None:
    reporter = current_search_status.get()
    if reporter is not None:
        reporter(message)
