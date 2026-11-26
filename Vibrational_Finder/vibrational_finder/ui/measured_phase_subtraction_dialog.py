from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from vibrational_finder.models import ObservedSpectrum, SpectralBand
from vibrational_finder.processing_state import ProcessingOperation, SpectrumProcessingState
from vibrational_finder.services.measured_phase_subtraction import (
    build_measured_phase_subtraction, prepare_measured_phase_profile,
)
from vibrational_finder.services.spectrum_processing import apply_processing_recipe
from vibrational_finder.ui.vibrational_plot import create_vibrational_plot_widget


class MeasuredPhaseSubtractionDialog(QDialog):
    def __init__(
        self, spectrum: ObservedSpectrum, references: Sequence[ObservedSpectrum],
        phase_name: str | None, phase_bands: Sequence[SpectralBand], other_bands: Sequence[SpectralBand],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Subtract measured phase" if phase_name else "Subtract measured spectrum")
        self.resize(960, 660)
        self.spectrum = spectrum
        self.phase_name = phase_name
        self.phase_bands, self.other_bands = phase_bands, other_bands
        self._references = references
        self._profile: ObservedSpectrum | None = None
        self._overlap = (0., 0.)
        self.operation: ProcessingOperation | None = None

        self.reference = QComboBox()
        self.reference.setMinimumContentsLength(22)
        self.reference.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for item in references:
            self.reference.addItem(item.name or item.id)
        self.strip_background = QCheckBox("Remove reference background")
        self.strip_background.setChecked(True)
        self.strip_background.setToolTip("Estimate the background of the measured reference only.")
        self.manual_range = QCheckBox("Manual fit range")
        self.lower, self.upper = QDoubleSpinBox(), QDoubleSpinBox()
        for field in (self.lower, self.upper):
            field.setDecimals(2)
            field.setSuffix(" cm⁻¹")
            field.setEnabled(False)
        limits = QHBoxLayout()
        limits.addWidget(self.manual_range)
        limits.addWidget(self.lower)
        limits.addWidget(self.upper)
        limits.addStretch()

        self.scale = QDoubleSpinBox()
        self.scale.setDecimals(8)
        self.scale.setRange(0., 1e10)
        self.scale.setSingleStep(.05)
        self.scale.setKeyboardTracking(False)
        self.scale.setToolTip("Intensity multiplier of the measured reference, not a phase fraction.")
        self.auto_fit = QPushButton("Fit scale")
        scale_row = QHBoxLayout()
        scale_row.addWidget(self.scale)
        scale_row.addWidget(self.auto_fit)
        scale_row.addStretch()

        fields = QFormLayout()
        if phase_name:
            fields.addRow("Accepted phase", QLabel(phase_name))
        fields.addRow("Measured reference", self.reference)
        fields.addRow("", self.strip_background)
        fields.addRow("Fit window", limits)
        fields.addRow("Scale", scale_row)
        self.plot = create_vibrational_plot_widget()
        self.plot.setMinimumHeight(300)
        self.plot.setTitle(None)
        self.plot.setLabel("bottom", "Raman shift", units="cm⁻¹")
        self.plot.setLabel("left", "Intensity", units=spectrum.y_unit)
        self.plot.addLegend(offset=(15, 10), labelTextColor="#111111", brush=(255, 255, 255, 220))
        self._order = np.argsort(spectrum.x)
        self._x = np.asarray(spectrum.x)[self._order]
        self._y = np.asarray(spectrum.y)[self._order]
        self.plot.plot(self._x, self._y, pen=pg.mkPen("#282c34", width=1.3), name="Input spectrum")
        self._component = self.plot.plot([], [], pen=pg.mkPen("#1a73e8", width=1.6), name="Scaled reference")
        self._residual = self.plot.plot(
            [], [], pen=pg.mkPen("#137658", width=1.55, style=Qt.PenStyle.DashLine), name="Residual",
        )
        self.status = QLabel()
        self.status.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("Save residual")
        self.save_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(fields)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        self.reference.currentIndexChanged.connect(self._reference_changed)
        self.strip_background.toggled.connect(self._reference_changed)
        self.manual_range.toggled.connect(self._toggle_range)
        self.auto_fit.clicked.connect(self.fit_scale)
        self.scale.valueChanged.connect(self._preview_manual)
        self._reference_changed()

    def _toggle_range(self, enabled: bool) -> None:
        self.lower.setEnabled(enabled)
        self.upper.setEnabled(enabled)

    def _reference_changed(self, *_args) -> None:
        self._profile = None
        self._clear_preview()
        with QSignalBlocker(self.scale):
            self.scale.setValue(0.)
        index = self.reference.currentIndex()
        if not 0 <= index < len(self._references):
            return
        try:
            profile = prepare_measured_phase_profile(
                self._references[index], remove_background=self.strip_background.isChecked(),
            )
            low, high = max(float(self._x[0]), profile.x[0]), min(float(self._x[-1]), profile.x[-1])
            if np.count_nonzero((self._x >= low) & (self._x <= high)) < 2:
                raise ValueError("The spectra have no usable common range.")
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self._profile, self._overlap = profile, (low, high)
        for field, value in ((self.lower, low), (self.upper, high)):
            field.setRange(low, high)
            field.setValue(value)
        self.fit_scale()

    def fit_scale(self) -> None:
        if self._profile is None:
            return
        fit_range = (self.lower.value(), self.upper.value()) if self.manual_range.isChecked() else None
        try:
            operation = build_measured_phase_subtraction(
                self.spectrum, self._profile, phase_name=self.phase_name, phase_bands=self.phase_bands,
                other_bands=self.other_bands, fit_range=fit_range,
            )
            with QSignalBlocker(self.scale):
                self.scale.setValue(float(operation.parameters["factor"]))
            self._preview(operation)
        except ValueError as exc:
            self._clear_preview()
            self.status.setText(str(exc))

    def _preview_manual(self, factor: float) -> None:
        if self._profile is None:
            return
        try:
            self._preview(build_measured_phase_subtraction(
                self.spectrum, self._profile, phase_name=self.phase_name, factor=factor,
            ))
        except ValueError as exc:
            self._clear_preview()
            self.status.setText(str(exc))

    def _clear_preview(self) -> None:
        self._component.setData([], [])
        self._residual.setData([], [])
        self.save_button.setEnabled(False)
        self.operation = None

    def _preview(self, operation: ProcessingOperation) -> None:
        if float(operation.parameters["factor"]) <= 0:
            self._clear_preview()
            self.status.setText("No positive scale was fitted. Choose another fit range or enter a scale.")
            return
        residual = apply_processing_recipe(self.spectrum, SpectrumProcessingState([operation]))
        values = np.asarray(residual.y)[self._order]
        self._residual.setData(self._x, values)
        lo, hi = self._overlap
        overlap = (self._x >= lo) & (self._x <= hi)
        self._component.setData(self._x[overlap], (self._y - values)[overlap])
        self.operation = operation
        self.save_button.setEnabled(True)
        windows = operation.parameters.get("fit_windows", ())
        label = f"Fit windows: {len(windows)}" if windows else (
            "Manual fit range" if "fit_range" in operation.parameters else "Manual scale"
        )
        self.status.setText(f"{label} · Subtraction range: {lo:.2f}–{hi:.2f} cm⁻¹")
