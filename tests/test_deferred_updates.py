from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from vibrational_finder.metadata import GITHUB_URL
from vibrational_finder.services import windows_update
from vibrational_finder.ui.deferred_updates import UpdateController, parse_update
from vibrational_finder.ui.project_document_actions import ProjectDocumentActionsMixin


VERSION = "1.0.1"
NAME = f"Raman_Phase_Finder_Setup_{VERSION}.exe"
URL = f"{GITHUB_URL}/releases/download/v{VERSION}/{NAME}"
BODY = b"MZ" + bytes(range(256)) * 8


def payload(**asset_changes):
    asset = {
        "type": "installer", "platform": "windows", "name": NAME, "url": URL,
        "sha256": sha256(BODY).hexdigest(), "size_bytes": len(BODY),
    }
    asset.update(asset_changes)
    return {
        "app_id": "ir_raman_phase_finder", "version": VERSION,
        "release_url": f"{GITHUB_URL}/releases/tag/v{VERSION}",
        "summary": ["Fix publication export"], "assets": [asset],
    }


class UpdateTests(unittest.TestCase):
    def test_only_the_verified_asset_for_this_release_is_eligible(self):
        valid = parse_update(payload(), current_version="1.0.0")
        self.assertEqual(valid.installer.url, URL)
        self.assertEqual(valid.summary, ("Fix publication export",))
        for changes in ({"sha256": ""}, {"size_bytes": -1}, {"url": URL.replace("v1.0.1", "v1.0.0")},
                        {"url": URL.replace("github.com", "example.org")}, {"platform": "macos"}):
            with self.subTest(changes=changes):
                self.assertIsNone(parse_update(payload(**changes), current_version="1.0.0").installer)
        self.assertIsNone(parse_update(payload(), current_version=VERSION))

    def test_download_reports_progress_and_checks_contents_before_final_rename(self):
        asset = parse_update(payload(), current_version="1.0.0").installer
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            progress = []
            with patch.object(windows_update, "_open_asset", return_value=BytesIO(BODY)):
                result = windows_update.download_installer(asset, root, progress.append, Event())
            self.assertEqual(result.name, NAME)
            self.assertEqual(result.read_bytes(), BODY)
            self.assertEqual(progress[-1], 100)
            self.assertEqual(list(root.iterdir()), [result])

    def test_mismatch_and_cancellation_do_not_leave_an_installer(self):
        asset = parse_update(payload(), current_version="1.0.0").installer
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for body in (BODY[:-1], BODY[:-1] + b"x", BODY + b"x"):
                with patch.object(windows_update, "_open_asset", return_value=BytesIO(body)):
                    with self.assertRaises(ValueError):
                        windows_update.download_installer(asset, root, lambda _: None, Event())
                self.assertEqual(list(root.iterdir()), [])
            stopped = Event()
            with patch.object(windows_update, "_open_asset", return_value=BytesIO(BODY)):
                with self.assertRaises(InterruptedError):
                    windows_update.download_installer(asset, root, lambda _: stopped.set(), stopped)
            self.assertEqual(list(root.iterdir()), [])

    def test_installer_does_not_start_when_the_user_cancels_project_close(self):
        window = ProjectDocumentActionsMixin()
        window.document = SimpleNamespace(dirty=True)
        window._project_file_operation = None
        window._confirmed_project_close = False
        window.close = Mock()
        launch = Mock(return_value=True)
        with patch.object(QMessageBox, "question", side_effect=[
            QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Discard,
        ]):
            self.assertFalse(window.request_update_install(launch))
            launch.assert_not_called()
            window.close.assert_not_called()
            self.assertTrue(window.request_update_install(launch))
            launch.assert_called_once_with()
            window.close.assert_called_once_with()
            self.assertTrue(window._confirmed_project_close)

    def test_startup_result_requires_explicit_confirmation_before_downloading(self):
        app = QApplication.instance() or QApplication([])
        window = QMainWindow()
        window.show()
        self.addCleanup(window.close)
        with TemporaryDirectory() as tmp:
            settings = QSettings(str(Path(tmp) / "updates.ini"), QSettings.Format.IniFormat)
            controller = UpdateController(window, settings)
            info = parse_update(payload(), current_version="1.0.0")
            with patch.dict(os.environ, {"IR_RAMAN_OFFLINE": "0"}):
                with patch("vibrational_finder.ui.deferred_updates.sys.platform", "win32"):
                    with patch.object(controller, "_confirm_install", side_effect=[False, True]) as ask:
                        with patch.object(controller, "_start_download") as start:
                            controller._complete(info, "")
                            start.assert_not_called()
                            controller._complete(info, "")
                            start.assert_called_once_with(info)
                    self.assertEqual(ask.call_count, 2)
