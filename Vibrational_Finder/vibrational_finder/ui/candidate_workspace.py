from __future__ import annotations

from collections.abc import Iterable
from math import isfinite
import re

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QActionGroup, QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from finder_core.models import CandidateRecord, SignalKind

from vibrational_finder.raman_gain import (
    GainWindow, RamanPeakPriority, compare_gain_bands, describe_gain_bands, gain_band_is_strong,
    priority_score, raman_priority_weights,
)
from vibrational_finder.models import SpectralBand, VibrationalMatchResult
from vibrational_finder.ui.managed_table import ManagedColumnTable
from vibrational_finder.ui.phase_color_delegate import PhaseColorDelegate


_NUMERIC_ROLE = Qt.ItemDataRole.UserRole + 1
_COMPOUND_COLUMN = 2
_LASER_COLUMN, _MATCH_COLUMN, _GAIN_COLUMN = 3, 4, 5
_UNSPECIFIED = "unspecified"


def _laser_nm(record: CandidateRecord) -> float | None:
    if record.kind == SignalKind.FTIR:
        return None
    for key in ("laser_nm", "excitation_wavelength_nm", "laser_wavelength_nm"):
        value = re.fullmatch(
            r"\s*(\d+(?:[.,]\d+)?)\s*(?:nm)?\s*(?:\([^()\d]*\))?\s*",
            str(record.metadata.get(key, "")), re.I,
        )
        if value:
            wavelength = float(value[1].replace(",", "."))
            if isfinite(wavelength) and wavelength > 0:
                return wavelength
    return None


class NumericTableItem(QTableWidgetItem):
    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = self.data(_NUMERIC_ROLE)
        right = other.data(_NUMERIC_ROLE)
        if left is None or right is None:
            return left is not None and right is None
        return float(left) < float(right)


class CandidateStatusLabel(QLabel):
    messagePublished = Signal(str)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text)
        self.messagePublished.emit(text)

    def set_routine_text(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text)
        self.messagePublished.emit("")


class CandidateWorkspace(QWidget):
    matchRequested = Signal(str)
    cancelRequested = Signal()
    previewRequested = Signal(str)
    acceptRequested = Signal(str)

    HEADERS = ("Source", "Formula", "Compound", "Laser (nm)", "Match", "Gain")
    HEADER_TOOLTIPS = (
        "Reference library or source",
        "Chemical formula",
        "Compound name or database entry",
        "Raman excitation wavelength in nm. Click the arrow to filter the results.",
        "Combined match score (0–100)",
        "Remaining unassigned bands explained after accepting a component. "
        "Automatic Raman: matched / remaining bands. Sorted by positional quality and coverage of both spectra. "
        "Not a phase fraction.",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("candidatePanel")
        self._results: dict[str, VibrationalMatchResult] = {}
        self._records: dict[str, CandidateRecord] = {}
        self._excitation_by_key: dict[str, float | None] = {}
        self._excitation_filter: float | str | None = None
        self._remaining_positions: set[float] | None = None
        self._accepted_keys: set[str] = set()
        self._accepted_colors: dict[str, str] = {}
        self._gain_bands: list[SpectralBand] | None = None
        self._gain_window = GainWindow()
        self._gain_priority_weights: tuple[float, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.search_controls = QWidget()
        self.search_controls.setObjectName("candidateSearchControls")
        query_row = QHBoxLayout(self.search_controls)
        query_row.setContentsMargins(0, 0, 0, 0)
        self.query_edit = QLineEdit()
        self.query_edit.setObjectName("candidateQueryEdit")
        self.query_edit.setPlaceholderText("Name, mineral or formula")
        self.match_button = QPushButton("Match")
        self.match_button.setObjectName("candidateMatchButton")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("candidateCancelButton")
        self.cancel_button.setEnabled(False)
        query_row.addWidget(self.query_edit, 1)
        query_row.addWidget(self.match_button)
        query_row.addWidget(self.cancel_button)
        layout.addWidget(self.search_controls)
        self.status_bar = QWidget()
        self.status_bar.setObjectName("candidateSearchStatus")
        status_row = QHBoxLayout(self.status_bar)
        status_row.setContentsMargins(8, 2, 8, 2)
        title = QLabel("Candidate list")
        status_row.addWidget(title)
        self.status_label = CandidateStatusLabel("Ready")
        self.status_label.setObjectName("candidateStatusLabel")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.status_label, 1)
        self.score_mode_label = QLabel("Match")
        status_row.addWidget(self.score_mode_label)
        layout.addWidget(self.status_bar)
        self.table = ManagedColumnTable(
            0, len(self.HEADERS), default_proportions=(0.15, 0.16, 0.31, 0.18, 0.10, 0.10)
        )
        self.table.setObjectName("candidateTable")
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        for column, tooltip in enumerate(self.HEADER_TOOLTIPS):
            self.table.horizontalHeaderItem(column).setToolTip(tooltip)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setToolTip("Click to preview a reference. Double-click to add it to the working set.")
        self.table.setItemDelegateForColumn(_COMPOUND_COLUMN, PhaseColorDelegate(self.table))
        self.table.set_column_layout_editable(False)
        self._candidate_menu = QMenu(self.table)
        self._candidate_menu.setObjectName("candidateActionsMenu")
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_candidate_menu)
        header = self.table.horizontalHeader()
        header.setSortIndicator(_MATCH_COLUMN, Qt.SortOrder.DescendingOrder)
        self.excitation_filter_button = QToolButton(header.viewport())
        self.excitation_filter_button.setObjectName("candidateExcitationFilter")
        self.excitation_filter_button.setText("▾")
        self.excitation_filter_button.setAccessibleName("Filter excitation wavelengths")
        self.excitation_filter_button.setAutoRaise(True)
        self.excitation_filter_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.excitation_filter_button.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        self.excitation_filter_menu = QMenu(self.excitation_filter_button)
        self._excitation_actions = QActionGroup(self.excitation_filter_menu)
        self._excitation_actions.setExclusive(True)
        self.excitation_filter_button.setMenu(self.excitation_filter_menu)
        header.geometriesChanged.connect(self._position_excitation_filter)
        header.sectionResized.connect(self._position_excitation_filter)
        header.sectionMoved.connect(self._position_excitation_filter)
        self._rebuild_excitation_menu()
        self._position_excitation_filter()
        layout.addWidget(self.table, 1)
        self.match_button.clicked.connect(lambda: self.matchRequested.emit(self.query_edit.text().strip()))
        self.query_edit.returnPressed.connect(lambda: self.matchRequested.emit(self.query_edit.text().strip()))
        self.cancel_button.clicked.connect(self.cancelRequested)
        self.table.cellClicked.connect(lambda row, _column: self._emit_key(self.previewRequested, row))
        self.table.cellDoubleClicked.connect(lambda row, _column: self._emit_key(self.acceptRequested, row))

    def candidate_key_at(self, row: int) -> str:
        item = self.table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""

    def _show_candidate_menu(self, position) -> None:
        item = self.table.itemAt(position)
        if item is None:
            return
        row = item.row()
        key = self.candidate_key_at(row)
        if not key:
            return
        self.table.selectRow(row)
        self._candidate_menu.clear()
        self._candidate_menu.addAction("Preview reference", lambda: self.previewRequested.emit(key))
        accept = self._candidate_menu.addAction("Add to working set", lambda: self.acceptRequested.emit(key))
        accept.setEnabled(key not in self._accepted_keys)
        self._candidate_menu.popup(self.table.viewport().mapToGlobal(position))

    def result_for_key(self, key: str) -> VibrationalMatchResult | None:
        return self._results.get(key)

    def record_for_key(self, key: str) -> CandidateRecord | None:
        return self._records.get(key)

    def _emit_key(self, signal: Signal, row: int) -> None:
        key = self.candidate_key_at(row)
        if key:
            signal.emit(key)

    @staticmethod
    def _number(value: float | None, *, compact: bool = False) -> NumericTableItem:
        if value is None:
            return NumericTableItem("—")
        value = float(value)
        item = NumericTableItem(f"{value:g}" if compact else f"{value:.1f}")
        item.setData(_NUMERIC_ROLE, value)
        return item

    def _position_excitation_filter(self, *_args) -> None:
        header = self.table.horizontalHeader()
        right = header.sectionViewportPosition(_LASER_COLUMN) + header.sectionSize(_LASER_COLUMN)
        height = max(12, min(22, header.viewport().height() - 4))
        self.excitation_filter_button.setGeometry(right - 22, (header.viewport().height() - height) // 2, 20, height)

    def _rebuild_excitation_menu(self) -> None:
        self.excitation_filter_menu.clear()
        values = set(self._excitation_by_key.values())
        options: list[tuple[str, float | str | None]] = [("All wavelengths", None)]
        options.extend((f"{nm:g} nm", nm) for nm in sorted(nm for nm in values if nm is not None))
        if None in values:
            options.append(("Unspecified", _UNSPECIFIED))
        if self._excitation_filter not in {value for _label, value in options}:
            self._excitation_filter = None
        for label, value in options:
            action = self.excitation_filter_menu.addAction(label)
            self._excitation_actions.addAction(action)
            action.setCheckable(True)
            action.setChecked(value == self._excitation_filter)
            action.triggered.connect(lambda _checked=False, selected=value: self._set_excitation_filter(selected))
        self.excitation_filter_button.setEnabled(len(options) > 1)
        self._update_excitation_filter_button()

    def _update_excitation_filter_button(self) -> None:
        active = self._excitation_filter
        label = f"{active:g} nm" if isinstance(active, float) else ("Unspecified" if active else "all wavelengths")
        self.excitation_filter_button.setText("▾" if active is None else "▿")
        self.excitation_filter_button.setToolTip(f"Filter excitation wavelengths: {label}")

    def _set_excitation_filter(self, wavelength: float | str | None) -> None:
        self._excitation_filter = wavelength
        self._update_excitation_filter_button()
        self._fill_table()

    def set_gain_context(
        self, remaining_positions: Iterable[float] | None, accepted_keys: Iterable[str], *,
        gain_bands: list[SpectralBand] | None = None,
        gain_window: GainWindow | None = None,
        peak_priority: RamanPeakPriority = RamanPeakPriority.OFF,
    ) -> None:
        remaining = None if remaining_positions is None else set(remaining_positions)
        accepted = set(accepted_keys)
        window = gain_window or GainWindow()
        weights = raman_priority_weights(gain_bands or (), window, peak_priority)
        if (remaining, accepted, gain_bands, window, weights) == (
            self._remaining_positions, self._accepted_keys, self._gain_bands, self._gain_window, self._gain_priority_weights,
        ):
            return
        stage_changed = bool(accepted) != bool(self._accepted_keys)
        self._remaining_positions, self._accepted_keys = remaining, accepted
        self._gain_bands = gain_bands
        self._gain_window = window
        self._gain_priority_weights = weights
        if accepted and gain_bands is not None and len(gain_bands) == 1:
            self.score_mode_label.setText("Gain · 1 band left · tentative")
        else:
            self.score_mode_label.setText(
                f"Gain · {len(remaining)} bands left" if accepted and remaining is not None
                else "Gain" if accepted else "Match"
            )
        if stage_changed:
            self.table.horizontalHeaderItem(_MATCH_COLUMN).setToolTip(
                "Match is inactive while phases are accepted." if accepted else self.HEADER_TOOLTIPS[_MATCH_COLUMN]
            )
        if not self._results:
            if stage_changed:
                self.table.sortItems(_GAIN_COLUMN if accepted else _MATCH_COLUMN, Qt.SortOrder.DescendingOrder)
            return
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        for row in range(self.table.rowCount()):
            key = self.candidate_key_at(row)
            self._set_score_items(row, key)
        if stage_changed:
            self.table.horizontalHeader().setSortIndicator(
                _GAIN_COLUMN if accepted else _MATCH_COLUMN, Qt.SortOrder.DescendingOrder
            )
        self.table.setSortingEnabled(sorting)

    def _set_score_items(self, row: int, key: str) -> None:
        result = self._results.get(key)
        match = (NumericTableItem("Inactive") if result is not None and self._accepted_keys
                 else self._number(result.score.combined if result is not None else None))
        self._set_row_item(row, _MATCH_COLUMN, match, key)
        self._set_row_item(row, _GAIN_COLUMN, self._gain_item(result), key)

    def _gain_item(self, result: VibrationalMatchResult | None) -> NumericTableItem:
        remaining = self._remaining_positions
        if remaining is None or result is None:
            return self._number(None)
        accepted = result.candidate.key in self._accepted_keys
        if self._gain_bands is not None:
            if not remaining or not result.reference_bands:
                return self._number(None)
            gain = None if accepted else compare_gain_bands(
                self._gain_bands, result.reference_bands, window=self._gain_window, use_evidence_confidence=True,
            )
            hits = len(gain.pairs) if gain is not None else 0
            item = NumericTableItem(f"{hits} / {len(remaining)}")
            score = priority_score(gain.quality, gain, self._gain_priority_weights) if gain is not None else 0.
            item.setData(_NUMERIC_ROLE, score)
            if gain is not None:
                tip = f"Positional Gain: {gain.quality:.1f} / 100 · {gain.eligible_reference_count} eligible reference bands"
                if score > gain.quality:
                    tip += f"\nGain with priority for isolated bands: {score:.1f} / 100"
                tip += "\nWith four or more residual bands, matches across the spectrum count more than matches in one region."
                evidence = describe_gain_bands(self._gain_bands, result.reference_bands, gain, window=self._gain_window)
                strong = {self._gain_bands[i].position for i, _, _ in gain.pairs if gain_band_is_strong(self._gain_bands[i])}
                if gain.strong_target:
                    tip += f"\nStrong bands matched: {len(strong)} · first-pass target: {gain.strong_target}"
                for label, positions in (
                    ("New strong bands", tuple(p for p in evidence.new_positions if p in strong)),
                    ("New weak bands (support only)", tuple(p for p in evidence.new_positions if p not in strong)),
                    ("In accepted or ambiguous windows (not counted)", evidence.masked_reference_positions),
                    ("Still unexplained", evidence.unexplained_positions),
                ):
                    text = ", ".join(f"{p:.1f}" for p in positions[:12]) or "—"
                    if len(positions) > 12:
                        text += f" (+{len(positions) - 12})"
                    tip += f"\n{label}: {text} cm⁻¹" if positions else f"\n{label}: {text}"
                if len(strong) == 1:
                    tip += "\nTentative: one strong matching band is not enough to confirm a phase."
                elif hits and not strong:
                    tip += "\nTentative: only weak bands match."
                item.setToolTip(tip)
            return item
        if not result.observed_bands:
            return self._number(None)
        if accepted or not remaining:
            return self._number(0.0)
        unassigned = {round(float(band.position), 6) for band in result.unassigned_bands}
        explained = {round(float(band.position), 6) for band in result.observed_bands} - unassigned
        new = sum(round(float(position), 6) in explained for position in remaining)
        return self._number(100.0 * new / len(remaining))

    def set_results(self, results: list[VibrationalMatchResult]) -> None:
        self._set_results(results, prefix="")

    def set_records(self, records: list[CandidateRecord]) -> None:
        self._results.clear()
        self._records = {record.key: record for record in records}
        self._refresh_table_records()
        self.status_label.set_routine_text(f"{len(records)} reference{'s' if len(records) != 1 else ''}")
        self._set_status_busy(False)
        self.match_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def set_residual_results(self, results: list[VibrationalMatchResult]) -> None:
        self._set_results(results, prefix="Next component — ")

    def _set_results(self, results: list[VibrationalMatchResult], *, prefix: str) -> None:
        self._records.clear()
        self._results = {result.candidate.key: result for result in results}
        self._refresh_table_records()
        self.status_label.set_routine_text(f"{prefix}{len(results)} candidate{'s' if len(results) != 1 else ''}")
        self._set_status_busy(False)
        self.match_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def _table_records(self) -> list[CandidateRecord]:
        return list(self._records.values()) or [result.candidate for result in self._results.values()]

    def _refresh_table_records(self) -> None:
        self._excitation_by_key = {record.key: _laser_nm(record) for record in self._table_records()}
        self._rebuild_excitation_menu()
        self._fill_table()

    def _set_row_item(self, row: int, column: int, item: QTableWidgetItem, key: str) -> None:
        item.setData(Qt.ItemDataRole.UserRole, key)
        if column == _COMPOUND_COLUMN:
            self._apply_accepted_color(item, key)
        self.table.setItem(row, column, item)

    def set_accepted_colors(self, colors: dict[str, str]) -> None:
        if colors == self._accepted_colors:
            return
        self._accepted_colors = dict(colors)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, _COMPOUND_COLUMN)
            if item is not None:
                self._apply_accepted_color(item, self.candidate_key_at(row))

    def _apply_accepted_color(self, item: QTableWidgetItem, key: str | None) -> None:
        color = QColor(self._accepted_colors.get(key or "", ""))
        item.setBackground(QBrush(color) if color.isValid() else QBrush())
        item.setToolTip(f"{item.text()} — accepted in this project" if color.isValid() else item.text())

    def _fill_table(self) -> None:
        active = self._excitation_filter
        records = self._table_records()
        if active is not None:
            value = None if active == _UNSPECIFIED else active
            records = [record for record in records if self._excitation_by_key[record.key] == value]
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            for column, value in enumerate((record.source, record.formula, record.name or record.entry_id)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self._set_row_item(row, column, item, record.key)
            self._set_row_item(row, _LASER_COLUMN, self._number(self._excitation_by_key[record.key], compact=True), record.key)
            self._set_score_items(row, record.key)
        self.table.setSortingEnabled(sorting)

    def _set_status_busy(self, busy: bool) -> None:
        if self.status_bar.property("searchBusy") == busy:
            return
        self.status_bar.setProperty("searchBusy", busy)
        for widget in (self.status_bar, *self.status_bar.findChildren(QLabel)):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

    def set_pending(self, *, gain: bool = False) -> None:
        self.status_label.setText("Updating Gain…" if gain else "Searching spectroscopy references…")
        self._set_status_busy(True)
        self.match_button.setEnabled(False)
        self.cancel_button.setEnabled(True)

    def set_failed(self, message: str) -> None:
        self.status_label.setText(f"{'Gain' if self._accepted_keys else 'Match'} failed: {message}")
        self._set_status_busy(False)
        self.match_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def set_unavailable(self, message: str) -> None:
        self._results.clear()
        self._records.clear()
        self._refresh_table_records()
        self.status_label.setText(message)
        self._set_status_busy(False)
        self.match_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def set_cancelled(self) -> None:
        self.status_label.setText(f"{'Gain' if self._accepted_keys else 'Match'} cancelled")
        self._set_status_busy(False)
        self.match_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

