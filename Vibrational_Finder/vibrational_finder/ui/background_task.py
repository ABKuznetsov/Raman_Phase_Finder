from __future__ import annotations

from collections.abc import Callable
from collections import deque
from threading import Event
import traceback

import time

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal

from vibrational_finder.cancellation import JobCancelled, checkpoint, current_cancel_event


class BackgroundTaskWorker(QObject):
    finished = Signal(object)
    failed = Signal(str, str)

    def __init__(self, task: Callable[[], object], cancel_event: Event) -> None:
        super().__init__()
        self._task = task
        self._cancel_event = cancel_event

    def run(self) -> None:
        token = current_cancel_event.set(self._cancel_event)
        try:
            checkpoint()
            result = self._task()
            checkpoint()
        except JobCancelled:
            pass
        except Exception as exc:
            self.failed.emit(str(exc), traceback.format_exc())
        else:
            self.finished.emit(result)
        finally:
            current_cancel_event.reset(token)
            QThread.currentThread().quit()


class BackgroundTaskHandle(QObject):
    finished = Signal(object)
    failed = Signal(str, str)
    drained = Signal()

    def __init__(self, task: Callable[[], object], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._active = True
        self._task = task
        self._cancel_event = Event()
        self._done = False
        self._thread.finished.connect(self._on_drained)

    def start(self) -> None:
        self._worker = BackgroundTaskWorker(self._task, self._cancel_event)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def cancel(self) -> None:
        """Suppress delivery and ask cooperative work to stop at its next checkpoint."""
        self._active = False
        self._cancel_event.set()

    def is_running(self) -> bool:
        return not self._done and self._thread.isRunning()

    def wait(self, timeout_ms: int = 5000) -> bool:
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
        app = QCoreApplication.instance()
        while self.is_running() and time.monotonic() < deadline:
            if app is not None:
                app.processEvents()
            if self.is_running():
                self._thread.wait(10)
        if app is not None:
            app.processEvents()
        return not self.is_running()

    def _on_finished(self, result: object) -> None:
        if self._active:
            self.finished.emit(result)

    def _on_failed(self, message: str, details: str) -> None:
        if self._active:
            self.failed.emit(message, details)

    def _on_drained(self) -> None:
        self._done = True
        self.drained.emit()


class BackgroundTaskLane:
    """One active worker; cancelled queued handles never start."""

    def __init__(self) -> None:
        self._running: BackgroundTaskHandle | None = None
        self._waiting: deque[BackgroundTaskHandle] = deque()

    def submit(self, handle: BackgroundTaskHandle) -> None:
        handle.drained.connect(lambda: self._retire(handle))
        self._waiting.append(handle)
        self._start_next()

    def cancel(self, handle: BackgroundTaskHandle) -> None:
        handle.cancel()
        if handle in self._waiting:
            self._waiting.remove(handle)
            handle._on_drained()

    def _start_next(self) -> None:
        if self._running is None and self._waiting:
            self._running = self._waiting.popleft()
            self._running.start()

    def _retire(self, handle: BackgroundTaskHandle) -> None:
        if handle is self._running:
            self._running = None
        handle.deleteLater()
        self._start_next()
