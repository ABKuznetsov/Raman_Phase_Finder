from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, Signal, Slot

from vibrational_finder.services.source_management import SpectroscopySourceManager
from vibrational_finder.ui.background_task import BackgroundTaskHandle, BackgroundTaskLane

if TYPE_CHECKING:
    from vibrational_finder.services.rruff_source import RruffArchiveInfo


@dataclass(frozen=True, slots=True)
class RruffManualDownload:
    archive: RruffArchiveInfo


class SourceManagementJobController(QObject):
    """Run one source mutation at a time and suppress cancelled delivery."""

    pending = Signal(str, str)
    succeeded = Signal(str, str, object)
    failed = Signal(str, str, str, str)
    downloadBlocked = Signal(str, object)
    cancelled = Signal(str, str)
    runningChanged = Signal(bool)
    progress = Signal(str, str)
    _worker_progress = Signal(object, str)

    def __init__(
        self,
        manager: SpectroscopySourceManager,
        parent: QObject | None = None,
        *,
        handle_factory: Callable[..., BackgroundTaskHandle] = BackgroundTaskHandle,
        lane: BackgroundTaskLane | None = None,
    ) -> None:
        super().__init__(parent)
        self.manager = manager
        self._handle_factory = handle_factory
        self._lane = lane if lane is not None else BackgroundTaskLane()
        self._current: BackgroundTaskHandle | None = None
        self._current_context: tuple[str, str] | None = None
        self._handles: list[BackgroundTaskHandle] = []
        self._worker_progress.connect(self._deliver_progress, Qt.ConnectionType.QueuedConnection)

    def start(
        self, name: str, operation: str, package_key: str | None = None, *, local_archive: str | None = None,
    ) -> BackgroundTaskHandle:
        self.cancel()
        manager = self.manager

        def task() -> object:
            return self._run(
                manager, name, operation, package_key, local_archive=local_archive,
                report_progress=lambda message: self._worker_progress.emit(handle, message),
            )

        handle = self._handle_factory(task, self)
        self._current = handle
        self._current_context = (name, operation)
        self._handles.append(handle)
        handle.finished.connect(lambda result, active=handle: self._finished(active, result))
        handle.failed.connect(lambda message, details, active=handle: self._failed(active, message, details))
        handle.drained.connect(lambda active=handle: self._drained(active))
        self.pending.emit(name, operation)
        self.runningChanged.emit(True)
        self._lane.submit(handle)
        return handle

    @staticmethod
    def _run(
        manager: SpectroscopySourceManager, name: str, operation: str, package_key: str | None, *,
        local_archive: str | None = None,
        report_progress: Callable[[str], None] | None = None,
    ) -> object:
        if operation == "update":
            from vibrational_finder.services.rruff_source import RruffDownloadBlocked

            try:
                return manager.update(name, package_key, report_progress=report_progress)
            except RruffDownloadBlocked as exc:
                return RruffManualDownload(exc.archive)
        if operation == "import_archive" and package_key is not None and local_archive is not None:
            return manager.import_archive(name, package_key, local_archive)
        if operation == "clear":
            return manager.clear(name, package_key)
        if operation == "build_index":
            return manager.build_index(name)
        if operation == "clear_index":
            return manager.clear_index(name)
        raise ValueError(f"Unknown source operation: {operation}")

    def cancel(self) -> None:
        if self._current is None:
            return
        handle = self._current
        context = self._current_context
        self._current = None
        self._current_context = None
        self._lane.cancel(handle)
        if context is not None:
            self.cancelled.emit(*context)
        self.runningChanged.emit(False)

    def is_running(self) -> bool:
        return self._current is not None

    @Slot(object, str)
    def _deliver_progress(self, handle: BackgroundTaskHandle, message: str) -> None:
        if handle is self._current and self._current_context is not None:
            self.progress.emit(self._current_context[0], message)

    def _finished(self, handle: BackgroundTaskHandle, result: object) -> None:
        if handle is not self._current or self._current_context is None:
            return
        name, operation = self._current_context
        self._current = None
        self._current_context = None
        self.runningChanged.emit(False)
        if isinstance(result, RruffManualDownload):
            self.downloadBlocked.emit(name, result.archive)
        else:
            self.succeeded.emit(name, operation, result)

    def _failed(self, handle: BackgroundTaskHandle, message: str, details: str) -> None:
        if handle is not self._current or self._current_context is None:
            return
        name, operation = self._current_context
        logging.getLogger(__name__).warning("%s %s failed: %s\n%s", name, operation, message, details)
        self._current = None
        self._current_context = None
        self.runningChanged.emit(False)
        self.failed.emit(name, operation, message, details)

    def _drained(self, handle: BackgroundTaskHandle) -> None:
        if handle in self._handles:
            self._handles.remove(handle)

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        self.cancel()
        handles = list(self._handles)
        for handle in handles:
            handle.cancel()
        if not handles:
            return True
        per_handle = max(1, timeout_ms // len(handles))
        results = [handle.wait(per_handle) for handle in handles]
        self._handles = [handle for handle in self._handles if handle.is_running()]
        return all(results)
