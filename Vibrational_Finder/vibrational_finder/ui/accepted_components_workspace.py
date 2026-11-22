from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QMenu, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from vibrational_finder.models import VibrationalMatchResult
from vibrational_finder.ui.managed_table import ManagedColumnTable
from vibrational_finder.ui.phase_color_delegate import PhaseColorDelegate


class AcceptedComponentsWorkspace(QWidget):
    visibilityChanged = Signal(str, bool)
    colorRequested = Signal(str)
    removeRequested = Signal(str)
    clearRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("acceptedComponentsPanel")
        self._visible_keys: set[str] = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.title_label = QLabel("Accepted results")
        self.title_label.setProperty("uiRole", "sectionTitle")
        layout.addWidget(self.title_label)
        self.table = ManagedColumnTable(0, 3, default_proportions=(0.13, 0.67, 0.20))
        self.table.setObjectName("acceptedComponentsTable")
        self.table.setHorizontalHeaderLabels(["Color", "Compound", "Match"])
        header_tooltips = (
            "Plot color. Double-click or use the context menu to change it.",
            "Accepted compound name",
            "Combined match score",
        )
        for column, tooltip in enumerate(header_tooltips):
            self.table.horizontalHeaderItem(column).setToolTip(tooltip)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.set_column_layout_editable(False)
        self.table.setItemDelegateForColumn(0, PhaseColorDelegate(self.table))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.cellDoubleClicked.connect(self._edit_color_cell)
        self._menu = QMenu(self.table)
        self._menu.setObjectName("acceptedResultsMenu")
        layout.addWidget(self.table, 1)

    def set_components(
        self,
        results: list[VibrationalMatchResult],
        visible_keys: set[str],
        colors: dict[str, str],
    ) -> None:
        self._visible_keys = set(visible_keys)
        self.table.setRowCount(len(results))
        for row, result in enumerate(results):
            key = result.candidate.key
            color = QTableWidgetItem()
            color.setBackground(QColor(colors.get(key, "#1a73e8")))
            color.setToolTip("Double-click to change the phase color")
            name = QTableWidgetItem(result.candidate.name or result.candidate.entry_id)
            suffix = "" if key in visible_keys else " (hidden on plot)"
            name.setToolTip(name.text() + suffix)
            if suffix:
                font = name.font()
                font.setItalic(True)
                name.setFont(font)
            score = QTableWidgetItem(f"{result.score.combined:.1f}")
            for column, item in enumerate((color, name, score)):
                item.setData(Qt.ItemDataRole.UserRole, key)
                self.table.setItem(row, column, item)

    def _edit_color_cell(self, row: int, column: int) -> None:
        item = self.table.item(row, 0)
        if column == 0 and item is not None:
            self.colorRequested.emit(item.data(Qt.ItemDataRole.UserRole))

    def _show_context_menu(self, position) -> None:
        item = self.table.itemAt(position)
        self._menu.clear()
        if item is not None:
            key = item.data(Qt.ItemDataRole.UserRole)
            self.table.selectRow(item.row())
            visible = key in self._visible_keys
            self._menu.addAction("Hide from plot" if visible else "Show on plot",
                                 lambda: self.visibilityChanged.emit(key, not visible))
            self._menu.addAction("Change color…", lambda: self.colorRequested.emit(key))
            self._menu.addSeparator()
            self._menu.addAction("Remove result", lambda: self.removeRequested.emit(key))
        if self.table.rowCount():
            self._menu.addAction("Clear accepted results", self.clearRequested.emit)
            self._menu.popup(self.table.viewport().mapToGlobal(position))

