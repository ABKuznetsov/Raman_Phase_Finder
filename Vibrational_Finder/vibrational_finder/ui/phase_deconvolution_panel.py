from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from vibrational_finder.models import SpectralBand
from vibrational_finder.services.phase_deconvolution import PhaseSubtractionFit, matched_reference_positions


class PhaseDeconvolutionPanel(QWidget):
    DISPLAY_COLUMNS = (
        ("Profile", "profile", False), ("Modes", "modes", False),
        ("Vibration", "assignments", False), ("Marker", "markers", True),
    )

    saveRequested = Signal()
    cancelRequested = Signal()
    chooseSpectrumRequested = Signal()
    layerToggled = Signal(str, str, bool)
    backgroundToggled = Signal(bool)
    displayChanged = Signal()

    def __init__(
        self, phases: dict[str, tuple[str, str, tuple[SpectralBand, ...]]],
        layer_settings: dict, *, background_visible: bool = True, experiment_visible: bool = False,
        residual_visible: bool = True,
    ) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        self.choose_spectrum_button = QPushButton("Choose spectrum to subtract…")
        self.choose_spectrum_button.clicked.connect(self.chooseSpectrumRequested)
        self.choose_spectrum_button.setToolTip("Use another measured spectrum. No reference card is required.")
        layout.addWidget(self.choose_spectrum_button)
        self.experiment_visible = QCheckBox("Experiment")
        self.residual_visible = QCheckBox("Residual (dashed)")
        self.experiment_visible.setChecked(experiment_visible)
        self.residual_visible.setChecked(residual_visible)
        self.background_visible = QCheckBox("Background")
        self.background_visible.setChecked(background_visible)
        self.background_visible.toggled.connect(self.backgroundToggled)
        display = QHBoxLayout()
        for box in (self.experiment_visible, self.residual_visible):
            box.toggled.connect(lambda _checked: self.displayChanged.emit())
            display.addWidget(box)
        display.addWidget(self.background_visible)
        layout.addLayout(display)
        table = QTableWidget(0, len(self.DISPLAY_COLUMNS) + 1, self)
        table.setObjectName("deconvolutionPhaseTable")
        self.phase_table = table
        table.setHorizontalHeaderLabels(("Phase", *(label for label, _key, _default in self.DISPLAY_COLUMNS)))
        table.horizontalHeaderItem(3).setToolTip("Vibration type")
        table.horizontalHeaderItem(4).setToolTip(
            "Split tick: reference (lower) and measured (upper) positions. "
            "Hollow diamond: reference position without a unique match.",
        )
        table.verticalHeader().hide()
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.set_phases(phases, layer_settings)
        table.itemChanged.connect(self._layer_changed)
        header.geometriesChanged.connect(self._fit_table_height)
        table.horizontalScrollBar().rangeChanged.connect(self._fit_table_height)
        layout.addWidget(table)
        row = QHBoxLayout()
        row.addStretch()
        self.save_button = QPushButton("Save spectrum")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.saveRequested)
        row.addWidget(self.save_button)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.cancelRequested)
        row.addWidget(close_button)
        layout.addLayout(row)
        self.status = QLabel("" if phases else "No card-based phases selected.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def set_phases(
        self, phases: dict[str, tuple[str, str, tuple[SpectralBand, ...]]], layer_settings: dict,
        *, fit: PhaseSubtractionFit | None = None,
    ) -> None:
        self.phases = phases
        self._phase_keys = tuple(phases)
        table = self.phase_table
        paired = matched_reference_positions(fit.bands) if fit is not None else {}
        contributing = {band.phase_key for band in fit.bands if band.removed and any(band.curve_y)} if fit else set()
        with QSignalBlocker(table):
            table.setRowCount(len(phases))
            for row, (key, (name, color, bands)) in enumerate(phases.items()):
                phase = QTableWidgetItem(name)
                phase.setFlags(Qt.ItemFlag.ItemIsEnabled)
                phase.setData(Qt.ItemDataRole.DecorationRole, QColor(color))
                phase.setToolTip(f"{name} · {len(bands)} reference bands")
                if fit is not None:
                    in_range = {band.position for band in bands if fit.x[0] <= band.position <= fit.x[-1]}
                    matches = paired.get(key, set())
                    # A verified pair can straddle a measured range boundary.
                    total = len(in_range | matches)
                    count = len(matches)
                    phase.setText(f"{name} ({count}/{total})")
                    phase.setToolTip(
                        f"{name} · {count} of {total} reference positions uniquely matched."
                        + (" No uniquely matched bands for subtraction." if not count else
                           " No contribution subtracted." if key not in contributing else "")
                    )
                table.setItem(row, 0, phase)
                for column, (label, _key, _default) in enumerate(self.DISPLAY_COLUMNS, 1):
                    item = QTableWidgetItem()
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setToolTip(f"Show {label.lower()} — {name}")
                    table.setItem(row, column, item)
            self.sync_layers(layer_settings)
        self._fit_table_height()
        table.setVisible(bool(phases))

    def _layer_changed(self, item: QTableWidgetItem) -> None:
        if item.column() > 0:
            field = self.DISPLAY_COLUMNS[item.column() - 1][1]
            self.layerToggled.emit(self._phase_keys[item.row()], field, item.checkState() == Qt.CheckState.Checked)

    def _fit_table_height(self, *_args) -> None:
        table = self.phase_table
        header = table.horizontalHeader()
        scrollbar = table.horizontalScrollBar()
        height = max(header.height(), header.sizeHint().height()) + 2 * table.frameWidth() + 4
        height += sum(table.rowHeight(row) for row in range(min(table.rowCount(), 6)))
        if scrollbar.maximum() > 0:
            height += scrollbar.sizeHint().height()
        if table.minimumHeight() != height:
            table.setFixedHeight(height)

    def sync_layers(self, layer_settings: dict) -> None:
        with QSignalBlocker(self.phase_table):
            for row, key in enumerate(self._phase_keys):
                options = layer_settings.get(key, {})
                for column, (_label, field, default) in enumerate(self.DISPLAY_COLUMNS, 1):
                    checked = options.get("visible", True) and options.get(field, default)
                    self.phase_table.item(row, column).setCheckState(
                        Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
                    )

    def update_fit(self, status: str, *, can_save: bool = False) -> None:
        self.status.setText(status)
        self.save_button.setEnabled(can_save)
