from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem


class PhaseColorDelegate(QStyledItemDelegate):
    """Draw the phase color over the table's row selection and stylesheet."""

    def paint(self, painter, option, index):
        brush = index.data(Qt.ItemDataRole.BackgroundRole)
        if not isinstance(brush, QBrush) or brush.style() == Qt.BrushStyle.NoBrush:
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        label, opt.text = opt.text, ""
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        painter.setClipRect(opt.rect)
        if label:
            painter.fillRect(opt.rect.adjusted(0, 0, 0, -1), brush)
            painter.setPen(QColor("#ffffff" if brush.color().lightness() < 150 else "#111111"))
            painter.setFont(opt.font)
            rect = opt.rect.adjusted(6, 0, -6, -1)
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                opt.fontMetrics.elidedText(label, opt.textElideMode, max(0, rect.width())),
            )
        else:
            painter.fillRect(opt.rect.adjusted(6, 5, -6, -5), brush)
        painter.restore()
