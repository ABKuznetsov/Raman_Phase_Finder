from __future__ import annotations

import os
from pathlib import Path
import sys


APP_DIRECTORY = "Raman Phase Finder"
_LEGACY_APP_DIRECTORIES = ("raman", "ir_raman_analysis_toolkit")


def default_sci_root() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Sci"
    if sys.platform == "darwin":
        override = os.environ.get("IR_RAMAN_SCI_ROOT")
        return Path(override) if override else Path.home() / "Library" / "Application Support" / "Sci"
    return Path.home() / ".local" / "share" / "Sci"


def _move_if_missing(old: Path, new: Path) -> None:
    if new.exists() or not old.is_dir():
        return
    try:
        old.rename(new)
    except OSError as exc:
        if not new.is_dir():
            raise OSError(f"Could not move {old} to {new}. Close the older Raman app and try again: {exc}") from exc


def raman_app_root(sci: Path | None = None) -> Path:
    apps = (sci if sci is not None else default_sci_root()) / "apps"
    root = apps / APP_DIRECTORY
    for previous_name in _LEGACY_APP_DIRECTORIES:
        legacy = apps / previous_name
        _move_if_missing(legacy, root)
        if root.is_dir() and legacy.is_dir():
            # Keep both copies if a directory already exists; SQLite files cannot be merged here.
            for name in ("data", "matplotlib", "settings"):
                _move_if_missing(legacy / name, root / name)
    migrate_qt_settings(root / "data")
    return root


def migrate_qt_settings(data: Path) -> Path:
    qt = data / "settings" / "qt"
    for previous_name in ("raman", "IRRamanPhaseFinder"):
        _move_if_missing(qt / previous_name, qt / APP_DIRECTORY)
    return qt


def raman_data_root() -> Path:
    configured = os.environ.get("IR_RAMAN_DATA_DIR")
    return Path(configured) if configured else raman_app_root() / "data"


def raman_logs_root(sci: Path | None = None) -> Path:
    logs = (sci if sci is not None else default_sci_root()) / "logs"
    root = logs / APP_DIRECTORY
    try:
        _move_if_missing(logs / "raman", root)
    except OSError:
        # Logs from a running older instance should not prevent startup.
        pass
    return root


if __name__ == "__main__":
    app = raman_app_root()
    for folder in ("data", "matplotlib", "settings"):
        (app / folder).mkdir(parents=True, exist_ok=True)
