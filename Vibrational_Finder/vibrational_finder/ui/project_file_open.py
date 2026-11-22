"""Queue native project-open events until the main window is ready."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QMessageBox

from vibrational_finder.io import PORTABLE_PROJECT_SUFFIX


class ProjectFileOpenHandler(QObject):
    def __init__(self, app) -> None:
        super().__init__(app)
        self.window = None
        self._paths: list[str] = []
        self._opening = False
        app.installEventFilter(self)

    def set_window(self, window) -> None:
        self.window = window
        QTimer.singleShot(0, self._open_next)

    def eventFilter(self, _watched, event) -> bool:  # noqa: N802 - Qt API
        if event.type() != QEvent.Type.FileOpen:
            return False
        path = event.file() or event.url().toLocalFile()
        if not path or Path(path).suffix.lower() != PORTABLE_PROJECT_SUFFIX:
            return False
        self.enqueue_path(path)
        event.accept()
        return True

    def enqueue_path(self, path: str | Path) -> None:
        resolved = str(Path(path).expanduser().resolve())
        if resolved not in self._paths:
            self._paths.append(resolved)
        QTimer.singleShot(0, self._open_next)

    def _open_finished(self) -> None:
        self._opening = False
        if self._paths:
            QTimer.singleShot(0, self._open_next)

    def _open_next(self) -> None:
        if self.window is None or self._opening or not self._paths:
            return
        path = Path(self._paths.pop(0))
        self._opening = True
        current = getattr(getattr(self.window, "document", None), "path", None)
        same = current is not None and Path(current).expanduser().resolve() == path
        self.window.setWindowState(self.window.windowState() & ~Qt.WindowState.WindowMinimized)
        self.window.raise_()
        self.window.activateWindow()
        request = getattr(self.window, "request_open_project_path", None)
        if not same and callable(request):
            if not request(path, done=self._open_finished):
                self._open_finished()
            return
        try:
            confirm = getattr(self.window, "_confirm_document_replacement", None)
            allowed = same or not callable(confirm) or confirm("open another project")
            if allowed and not same:
                try:
                    self.window.open_project_path(path)
                except (OSError, ValueError) as exc:
                    QMessageBox.warning(self.window, "Open project", f"Could not open project:\n{path}\n\n{exc}")
        finally:
            self._open_finished()
