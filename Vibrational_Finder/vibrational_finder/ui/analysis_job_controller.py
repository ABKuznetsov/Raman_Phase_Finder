from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

from vibrational_finder.services.search_progress import current_search_status
from vibrational_finder.ui.background_task import BackgroundTaskHandle, BackgroundTaskLane


class AnalysisJobController(QObject):
    """Own Match workers and suppress cancelled or revision-stale outcomes."""

    pending = Signal(object)
    progress = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str, str)
    failed_for_request = Signal(object, str, str)
    cancelled = Signal()
    stale = Signal(object)
    runningChanged = Signal(bool)
    statusChanged = Signal(str)
    _worker_status = Signal(object, str)

    def __init__(
        self,
        analysis_controller: Any,
        parent: QObject | None = None,
        *,
        handle_factory: Callable[..., BackgroundTaskHandle] = BackgroundTaskHandle,
        lane: BackgroundTaskLane | None = None,
    ) -> None:
        super().__init__(parent)
        self.analysis_controller = analysis_controller
        self._handle_factory = handle_factory
        self._lane = lane if lane is not None else BackgroundTaskLane()
        self._current: BackgroundTaskHandle | None = None
        self._handles: list[BackgroundTaskHandle] = []
        self._worker_status.connect(self._deliver_status, Qt.ConnectionType.QueuedConnection)

    def start(self, request: object) -> BackgroundTaskHandle:
        if self._current is not None:
            self._cancel_current(emit_signal=True)
        controller = self.analysis_controller

        def run() -> object:
            token = current_search_status.set(lambda message: self._worker_status.emit(handle, message))
            try:
                return controller.run(request)
            finally:
                current_search_status.reset(token)

        handle = self._handle_factory(run, self)
        self._current = handle
        self._handles.append(handle)
        handle.finished.connect(lambda outcome, active=handle: self._finished(active, outcome))
        handle.failed.connect(lambda message, details: self._failed(handle, request, message, details))
        handle.drained.connect(lambda active=handle: self._drained(active))
        self.pending.emit(request)
        self.progress.emit(0, "Searching spectroscopy references")
        self.runningChanged.emit(True)
        self._lane.submit(handle)
        return handle

    def cancel(self) -> None:
        self._cancel_current(emit_signal=True)

    def _cancel_current(self, *, emit_signal: bool) -> None:
        handle = self._current
        if handle is None:
            return
        self._current = None
        self._lane.cancel(handle)
        if emit_signal:
            self.cancelled.emit()
        self.runningChanged.emit(False)

    def is_running(self) -> bool:
        return self._current is not None

    @Slot(object, str)
    def _deliver_status(self, handle: BackgroundTaskHandle, message: str) -> None:
        if handle is self._current:
            self.statusChanged.emit(message)

    def _finished(self, handle: BackgroundTaskHandle, outcome: object) -> None:
        if handle is not self._current:
            return
        self._current = None
        self.runningChanged.emit(False)
        if self.analysis_controller.apply_if_current(outcome):
            self.progress.emit(100, "Match complete")
            self.succeeded.emit(outcome)
        else:
            self.stale.emit(outcome)

    def _failed(self, handle: BackgroundTaskHandle, request: object, message: str, details: str) -> None:
        if handle is not self._current:
            return
        self._current = None
        self.runningChanged.emit(False)
        self.failed.emit(message, details)
        self.failed_for_request.emit(request, message, details)

    def _drained(self, handle: BackgroundTaskHandle) -> None:
        if handle in self._handles:
            self._handles.remove(handle)

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        self._cancel_current(emit_signal=False)
        handles = list(self._handles)
        for handle in handles:
            handle.cancel()
        if not handles:
            return True
        per_handle = max(1, timeout_ms // len(handles))
        drained_results = [handle.wait(per_handle) for handle in handles]
        self._handles = [handle for handle in self._handles if handle.is_running()]
        return all(drained_results)
