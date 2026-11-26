from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from vibrational_finder.core.instrument_profile import InstrumentProfile
from vibrational_finder.ui.instrument_profile_store import InstrumentProfileStore


class InstrumentProfilePanel(QWidget):
    profileChanged = Signal(object)

    def __init__(self, store: InstrumentProfileStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setObjectName("instrumentProfilePanel")
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.addWidget(QLabel("Active instrument profile"))
        self.profile_combo = QComboBox()
        self.profile_combo.setAccessibleName("Active instrument profile")
        self.profile_combo.setMinimumContentsLength(16)
        self.profile_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        root.addWidget(self.profile_combo)

        actions = QHBoxLayout()
        self.new_button = QPushButton("New...")
        self.edit_button = QPushButton("Edit...")
        self.delete_button = QPushButton("Delete")
        for button in (self.new_button, self.edit_button, self.delete_button):
            actions.addWidget(button)
        root.addLayout(actions)
        self.laser_label = QLabel()
        self.laser_label.setWordWrap(True)
        self.model_label = QLabel()
        self.model_label.setWordWrap(True)
        self.model_label.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.laser_label)
        root.addWidget(self.model_label)
        root.addStretch(1)

        self.new_button.clicked.connect(lambda: self._edit_profile(None))
        self.edit_button.clicked.connect(lambda: self._edit_profile(self.store.active))
        self.delete_button.clicked.connect(self._delete_profile)
        self.profile_combo.currentIndexChanged.connect(self._select_profile)
        self.refresh()

    def refresh(self) -> None:
        profile = self.store.active
        with QSignalBlocker(self.profile_combo):
            self.profile_combo.clear()
            self.profile_combo.addItem("No instrument profile", None)
            for entry in self.store.profiles:
                self.profile_combo.addItem(f"{entry.name} ({entry.laser_nm:g} nm)", entry.profile_id)
            self.profile_combo.setCurrentIndex(self.profile_combo.findData(profile.profile_id) if profile else 0)
        self.edit_button.setEnabled(profile is not None)
        self.delete_button.setEnabled(profile is not None)
        self.laser_label.setText(f"Laser: {profile.laser_nm:g} nm (±{profile.tolerance_nm:g} nm)" if profile else "")
        self.laser_label.setVisible(profile is not None)
        details = []
        if profile is not None:
            details.extend(f"{label}: {value}" for label, value in (
                ("Model", profile.model), ("Objective", profile.objective), ("Grating", profile.grating),
            ) if value)
            if profile.resolution_cm1 is not None:
                details.append(f"Resolution: {profile.resolution_cm1:g} cm⁻¹")
        self.model_label.setText("\n".join(details))
        self.model_label.setVisible(bool(details))

    def _select_profile(self, _index: int) -> None:
        self.store.select(self.profile_combo.currentData())
        self.refresh()
        self.profileChanged.emit(self.store.active)

    def _edit_profile(self, original: InstrumentProfile | None) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit instrument profile" if original else "New instrument profile")
        dialog.setMinimumWidth(380)
        root = QVBoxLayout(dialog)
        form = QFormLayout()
        root.addLayout(form)

        name = QLineEdit(original.name if original else "")
        name.setMaxLength(80)
        model = QLineEdit(original.model if original else "")
        objective = QLineEdit(original.objective if original else "")
        grating = QLineEdit(original.grating if original else "")
        laser = _spin(1., 10000., original.laser_nm if original else 532., " nm")
        tolerance = _spin(0., 100., original.tolerance_nm if original else 2., " nm")
        resolution = _spin(0., 1000., (original.resolution_cm1 or 0.) if original else 0., " cm⁻¹")
        resolution.setSpecialValueText("Unspecified")
        for label, field in (
            ("Profile name", name), ("Laser", laser), ("Laser tolerance", tolerance),
            ("Instrument model", model), ("Objective", objective), ("Grating", grating),
            ("Spectral resolution", resolution),
        ):
            form.addRow(label, field)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        root.addWidget(buttons)

        def save() -> None:
            try:
                profile = InstrumentProfile(
                    profile_id=original.profile_id if original else uuid4().hex,
                    name=name.text().strip(), laser_nm=laser.value(), tolerance_nm=tolerance.value(),
                    model=model.text().strip(), objective=objective.text().strip(), grating=grating.text().strip(),
                    resolution_cm1=resolution.value() or None,
                )
                self.store.save(profile, select=True)
            except ValueError as exc:
                QMessageBox.warning(dialog, "Instrument profile", str(exc))
                return
            dialog.accept()

        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        name.setFocus()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()
            self.profileChanged.emit(self.store.active)

    def _delete_profile(self) -> None:
        profile = self.store.active
        if profile is None:
            return
        if QMessageBox.question(
            self, "Delete instrument profile", f"Delete the profile '{profile.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self.store.delete(profile.profile_id)
            self.refresh()
            self.profileChanged.emit(self.store.active)


def _spin(minimum: float, maximum: float, value: float, suffix: str) -> QDoubleSpinBox:
    control = QDoubleSpinBox()
    control.setRange(minimum, maximum)
    control.setDecimals(2)
    control.setSingleStep(0.5)
    control.setSuffix(suffix)
    control.setValue(value)
    return control
