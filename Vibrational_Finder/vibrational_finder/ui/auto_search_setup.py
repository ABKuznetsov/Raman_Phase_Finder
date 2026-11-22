"""Optional Raman search settings, anchored to the Identify split button."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from vibrational_finder.raman_gain import RamanPeakPriority


_SETTING = "auto_search/raman_peak_priority"
PEAK_PRIORITY_LABELS = {
    RamanPeakPriority.OFF: "Off",
    RamanPeakPriority.POSITION: "Peak positions",
    RamanPeakPriority.INTENSITY: "Measured intensity",
}


class AutoSearchSetup(QFrame):
    def __init__(self, settings: QSettings, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("autoSearchSetup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(330)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        header = QHBoxLayout()
        title = QLabel("Identify compounds")
        title.setProperty("uiRole", "sectionTitle")
        header.addWidget(title, 1)
        self.close_button = QToolButton()
        self.close_button.setText("×")
        self.close_button.setAccessibleName("Close search settings")
        self.close_button.clicked.connect(self.hide)
        header.addWidget(self.close_button)
        layout.addLayout(header)

        inventory = QHBoxLayout()
        cards = QLabel("Local cards available")
        inventory.addWidget(cards, 1)
        self.card_count = QLabel("—")
        self.card_count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        info = ("Cards from enabled local libraries for the current method, before search filters. "
                "Different reference cards for the same phase are counted separately.")
        cards.setToolTip(info)
        self.card_count.setToolTip(info)
        inventory.addWidget(self.card_count)
        layout.addLayout(inventory)

        title = QLabel("Additional Raman search pass")
        title.setProperty("uiRole", "sectionTitle")
        layout.addWidget(title)
        self.priority_combo = QComboBox()
        self.priority_combo.setAccessibleName("Raman peak priority")
        for priority, label in PEAK_PRIORITY_LABELS.items():
            self.priority_combo.addItem(label, priority.value)
        index = self.priority_combo.findData(settings.value(_SETTING, RamanPeakPriority.OFF.value, type=str))
        self.priority_combo.setCurrentIndex(max(index, 0))
        self.priority_combo.currentIndexChanged.connect(
            lambda _index: settings.setValue(_SETTING, self.peak_priority.value)
        )
        layout.addWidget(self.priority_combo)
        note = QLabel(
            "An extra pass favors isolated bands by position or by their prominence above the measured background. "
            "Reference intensities are not used. Click Identify compounds to run."
        )
        note.setMaximumWidth(360)
        note.setWordWrap(True)
        note.setProperty("uiRole", "muted")
        layout.addWidget(note)

    @property
    def peak_priority(self) -> RamanPeakPriority:
        return RamanPeakPriority(self.priority_combo.currentData())

    def show_below(self, anchor: QWidget, cards: int | None) -> None:
        self.card_count.setText(f"{cards:,}" if cards is not None else "Unavailable")
        self.adjustSize()
        screen = anchor.screen().availableGeometry()
        point = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        self.move(max(screen.left(), min(point.x(), screen.right() - self.width() + 1)),
                  max(screen.top(), min(point.y(), screen.bottom() - self.height() + 1)))
        self.show()
        self.priority_combo.setFocus(Qt.FocusReason.PopupFocusReason)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)
