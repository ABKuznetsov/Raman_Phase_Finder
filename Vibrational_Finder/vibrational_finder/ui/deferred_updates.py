"""Check for releases after startup and offer verified Windows updates."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
import os
from pathlib import Path
import re
import sys
from threading import Event, Thread
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from vibrational_finder.core.app_paths import APP_DIRECTORY, default_sci_root
from vibrational_finder.metadata import APP_NAME, APP_VERSION, GITHUB_URL, UPDATE_MANIFEST_URL
from vibrational_finder.services.windows_update import WindowsInstaller, download_installer, windows_release_installer
from vibrational_finder.ui.app_settings import app_settings

RELEASES_URL = f"{GITHUB_URL}/releases"
MANIFEST_URL = UPDATE_MANIFEST_URL


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    version: str
    url: str
    installer: WindowsInstaller | None = None
    summary: tuple[str, ...] = ()


def _version(value: object) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d{1,8})\.(\d{1,8})\.(\d{1,8})", str(value))
    return tuple(int(part) for part in match.groups()) if match else None


def parse_update(payload: dict, *, current_version: str = APP_VERSION) -> UpdateInfo | None:
    apps = payload.get("apps")
    if isinstance(apps, dict):
        payload = apps.get("ir_raman_phase_finder", {})
    if not isinstance(payload, dict):
        return None
    remote = _version(payload.get("version"))
    current = _version(current_version)
    if remote is None or current is None or remote <= current:
        return None
    url = str(payload.get("release_page_url") or payload.get("release_url") or RELEASES_URL)
    try:
        address = urlsplit(url)
    except ValueError:
        address = urlsplit(RELEASES_URL)
        url = RELEASES_URL
    official_path = urlsplit(RELEASES_URL).path
    if address.scheme != "https" or address.netloc != "github.com" or (
        address.path != official_path and not address.path.startswith(f"{official_path}/")
    ):
        url = RELEASES_URL
    version = ".".join(map(str, remote))
    lines = payload.get("summary")
    summary = tuple(line.strip()[:240] for line in lines[:5]
                    if isinstance(line, str) and line.strip()) if isinstance(lines, list) else ()
    installer = windows_release_installer(payload.get("assets"), version)
    return UpdateInfo(version, url, installer, summary)


def fetch_update() -> UpdateInfo | None:
    import json
    import ssl
    from urllib.request import Request, urlopen

    import certifi

    request = Request(MANIFEST_URL, headers={"User-Agent": f"Raman-Phase-Finder/{APP_VERSION}"})
    maximum = 2 * 1024 * 1024
    with urlopen(request, timeout=4, context=ssl.create_default_context(cafile=certifi.where())) as reply:
        raw = reply.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("The release manifest is too large.")
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("Invalid release manifest.")
    return parse_update(payload)


def _offline() -> bool:
    return os.environ.get("IR_RAMAN_OFFLINE", "").strip().casefold() in {"1", "true", "yes", "on"}


class UpdateController(QObject):
    finished = Signal(object, str)
    download_progress = Signal(int)
    download_finished = Signal(object, str)

    def __init__(self, window, settings=None) -> None:
        super().__init__(window)
        self.window = window
        self.settings = settings if settings is not None else app_settings()
        self.setting_key = f"updates/raman_standalone/last_check/{APP_VERSION}"
        self.result: UpdateInfo | None = None
        self.busy = self.manual = False
        self._download_dialog: QProgressDialog | None = None
        self._download_cancelled = Event()
        self._download_info: UpdateInfo | None = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.check)
        self.finished.connect(self._complete)
        self.download_progress.connect(self._set_download_progress)
        self.download_finished.connect(self._finish_download)

    def schedule(self, delay_ms: int = 5000) -> None:
        self.timer.start(delay_ms)

    def check(self, *, manual: bool = False) -> None:
        if _offline():
            if manual:
                self._show("Offline mode is enabled. Updates were not checked.")
            return
        if self._download_info is not None:
            return
        if self.busy:
            self.manual |= manual
            return
        try:
            last = float(self.settings.value(self.setting_key, 0))
        except (TypeError, ValueError):
            last = 0
        now = time.time()
        if not manual and 0 <= now - last < 86400:
            return
        self.settings.setValue(self.setting_key, now)
        self.busy = True
        self.manual = manual

        def work() -> None:
            info, error = None, ""
            try:
                info = fetch_update()
            except Exception as exc:
                error = str(exc) or type(exc).__name__
            try:
                self.finished.emit(info, error)
            except RuntimeError:
                pass  # Qt may already have destroyed the window.

        Thread(target=work, name="ir-raman-update-check", daemon=True).start()

    @Slot(object, str)
    def _complete(self, info: UpdateInfo | None, error: str) -> None:
        self.busy = False
        self.result = info
        if not self.window.isVisible() or _offline():
            return
        if info is not None:
            action = getattr(self.window, "check_updates_action", None)
            if action is not None:
                action.setText(f"Update available: {info.version}…")
            self.window.statusBar().showMessage(
                f"{APP_NAME} {info.version} is available — see Help.", 15000,
            )
            if sys.platform == "win32" and info.installer is not None:
                if self._confirm_install(info):
                    self._start_download(info)
                return
        if self.manual:
            if error:
                text = f"Could not check for updates: {escape(error)}"
            elif info is not None:
                text = f"{APP_NAME} {info.version} is available. <a href='{escape(info.url, quote=True)}'>View release</a>."
            else:
                text = f"{APP_NAME} {APP_VERSION} is up to date."
            self._show(text)

    def _confirm_install(self, info: UpdateInfo) -> bool:
        message = QMessageBox(self.window)
        message.setWindowTitle(f"{APP_NAME} update")
        message.setIcon(QMessageBox.Icon.Question)
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setText(f"{APP_NAME} {info.version} is available. Installed version: {APP_VERSION}.")
        details = "\n".join(f"• {line}" for line in info.summary)
        message.setInformativeText("\n\n".join(filter(None, (details, "Download and start the installer?"))))
        install = message.addButton("Download and install", QMessageBox.ButtonRole.AcceptRole)
        later = message.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        message.setDefaultButton(later)
        message.exec()
        return message.clickedButton() is install

    def _start_download(self, info: UpdateInfo) -> None:
        asset = info.installer
        if asset is None or self._download_info is not None:
            return
        self._download_info = info
        cancelled = self._download_cancelled = Event()
        dialog = self._download_dialog = QProgressDialog(
            f"Downloading {APP_NAME} {info.version}…", "Cancel", 0, 100, self.window,
        )
        dialog.setWindowTitle(f"{APP_NAME} update")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(cancelled.set)
        dialog.setValue(0)
        dialog.show()

        def work() -> None:
            path, error = None, ""
            try:
                path = download_installer(
                    asset, default_sci_root() / "updates" / APP_DIRECTORY, self.download_progress.emit, cancelled,
                )
            except Exception as exc:
                error = str(exc) or type(exc).__name__
            try:
                self.download_finished.emit(path, error)
            except RuntimeError:
                pass

        Thread(target=work, name="raman-update-download", daemon=True).start()

    @Slot(int)
    def _set_download_progress(self, value: int) -> None:
        if self._download_dialog is not None and not self._download_cancelled.is_set():
            self._download_dialog.setValue(value)

    @Slot(object, str)
    def _finish_download(self, path: Path | None, error: str) -> None:
        info, self._download_info = self._download_info, None
        dialog, self._download_dialog = self._download_dialog, None
        if dialog is not None:
            dialog.reset()
            dialog.deleteLater()
        if info is None or self._download_cancelled.is_set() or not self.window.isVisible():
            return
        if path is None:
            self._offer_release_page(info, f"Could not download the update: {error}")
            return
        self.window.request_update_install(lambda: self._launch_installer(path, info))

    def _launch_installer(self, path: Path, info: UpdateInfo) -> bool:
        try:
            os.startfile(str(path), "runas")
        except OSError as exc:
            if getattr(exc, "winerror", None) != 1223:  # UAC was declined.
                self._offer_release_page(info, f"Could not start the installer: {exc}")
            return False
        return True

    def _offer_release_page(self, info: UpdateInfo, error: str) -> None:
        answer = QMessageBox.question(
            self.window, f"{APP_NAME} update", f"{error}\n\nOpen the release page instead?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl(info.url))

    def _show(self, text: str) -> None:
        message = QMessageBox(self.window)
        message.setWindowTitle("Updates")
        message.setTextFormat(Qt.TextFormat.RichText)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        message.setText(text)
        message.setStandardButtons(QMessageBox.StandardButton.Ok)
        message.exec()


def update_controller(window) -> UpdateController:
    controller = getattr(window, "_update_controller", None)
    if controller is None:
        controller = UpdateController(window)
        window._update_controller = controller
    return controller
