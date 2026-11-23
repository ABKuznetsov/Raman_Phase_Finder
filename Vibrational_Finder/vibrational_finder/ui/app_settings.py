from __future__ import annotations

from PySide6.QtCore import QSettings

from vibrational_finder.core.app_paths import APP_DIRECTORY, migrate_qt_settings, raman_data_root


def configure_app_settings_storage() -> None:
    settings_root = migrate_qt_settings(raman_data_root())
    settings_root.mkdir(parents=True, exist_ok=True)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(settings_root),
    )


def app_settings() -> QSettings:
    configure_app_settings_storage()
    return QSettings(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        APP_DIRECTORY,
        "Standalone",
    )
