from __future__ import annotations

import math
from html import escape

import pyqtgraph as pg
from PySide6.QtCore import QLineF, QRectF, Qt
from PySide6.QtGui import QColor, QFont

from finder_core.models import SignalKind
from vibrational_finder.plot_export.metadata import CanvasLayer, canvas_item_tag, tag_canvas_item
from vibrational_finder.ui.plot_view_settings import PlotViewSettings, plot_style_from_view_settings


def _color(value: str, fallback: str) -> QColor:
    color = QColor(str(value))
    return color if color.isValid() else QColor(fallback)


class SpectrumGrid(pg.GraphicsObject):
    """Draw the same tick-aligned, independently styled grid as XRD."""

    def __init__(self, plot) -> None:
        super().__init__()
        self.view = plot.getViewBox()
        self.axes = (plot.getAxis("bottom"), plot.getAxis("left"))
        self.rect = QRectF()
        self.pen = pg.mkPen("#8f969e")
        self.setZValue(-1000)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.view.addItem(self, ignoreBounds=True)
        self.view.sigRangeChanged.connect(self.refresh)
        self.view.sigResized.connect(self.refresh)
        tag_canvas_item(self, layer=CanvasLayer.GRID, object_id="spectrum-grid")

    def refresh(self, *_args) -> None:
        self.prepareGeometryChange()
        self.rect = self.view.viewRect().normalized()
        self.update()

    def boundingRect(self) -> QRectF:
        return QRectF(self.rect)

    def paint(self, painter, _option, _widget=None) -> None:
        rect = self.rect
        if rect.isEmpty():
            return
        painter.setPen(self.pen)
        for axis, (low, high), pixels in zip(
            self.axes, ((rect.left(), rect.right()), (rect.top(), rect.bottom())),
            (self.view.width(), self.view.height()), strict=True,
        ):
            ticks = axis.tickValues(low, high, max(pixels, 1))
            for value in sorted({value for _spacing, level in ticks[:2] for value in level if low < value < high}):
                if axis is self.axes[0]:
                    painter.drawLine(QLineF(value, rect.top(), value, rect.bottom()))
                else:
                    painter.drawLine(QLineF(rect.left(), value, rect.right(), value))


def apply_spectrum_appearance(
    plot, settings: PlotViewSettings, *, title: str, x_label: str, y_label: str, y_unit: str, all_black: bool,
    wavelength_context: tuple[SignalKind, float] | None = None,
) -> None:
    plot.setBackground(_color(settings.plot_background, "#ffffff"))
    plot.plotItem.layout.setContentsMargins(6, 6, 6, 6)
    view = plot.getViewBox()
    border = pg.mkPen(_color(settings.plot_border_color, "#111111"), width=settings.plot_border_width)
    view.setBorder(border if settings.plot_border_visible and settings.plot_border_width else None)
    tag_canvas_item(view.borderRect, layer=CanvasLayer.AXES, object_id="spectrum-frame")
    # PlotItem collapses its fixed title row only when the title is None.
    plot.setTitle(escape(settings.title_text or title) if settings.title_visible else None,
                  color=_color(settings.title_color, "#111111"),
                  size=f"{settings.title_font_size}pt")
    font = QFont()
    font.setPointSize(max(1, min(72, int(settings.tick_font_size))))
    axis_color = _color(settings.axis_color, "#111111")
    for name in ("bottom", "top", "left", "right"):
        horizontal = name in {"bottom", "top"}
        axis = plot.getAxis(name)
        visible = getattr(settings, f"{name}_axis_visible")
        plot.showAxis(name, visible)
        axis.setPen(pg.mkPen(axis_color, width=settings.axis_width))
        axis.setTextPen(axis_color)
        axis.setTickFont(font)
        axis.setStyle(showValues=getattr(settings, f"{name}_axis_values_visible"),
                      tickLength=-abs(settings.tick_length))
        label = getattr(settings, f"{name}_axis_label") or (x_label if horizontal else y_label)
        unit = getattr(settings, f"{name}_axis_unit") or y_unit
        if horizontal:
            context = wavelength_context if getattr(settings, f"{name}_axis_scale") == "nm" else None
            axis.set_wavelength_context(context)
            unit = "nm" if context is not None else "cm⁻¹"
            if context is not None and not getattr(settings, f"{name}_axis_label"):
                label = "Scattered wavelength" if context[0] == SignalKind.RAMAN else "Wavelength"
        # Disabling alone can recalculate a non-unit SI factor for a visible axis.
        axis.setLabel(escape(label), units=escape(unit), siPrefixEnableRanges=(),
                      color=axis_color.name(), **{"font-size": f"{settings.label_font_size}pt"})
        axis.enableAutoSIPrefix(False)
        axis.showLabel(bool(visible and getattr(settings, f"{name}_axis_label_visible")))
        major, minor = ((settings.x_major_tick_spacing, settings.x_minor_tick_spacing) if horizontal
                        else (settings.y_major_tick_spacing, settings.y_minor_tick_spacing))
        steps = sorted({step for step in (major, minor) if math.isfinite(step) and step > 0}, reverse=True)
        axis.setTickSpacing(levels=[(step, 0) for step in steps] if steps else None)

    plot.showGrid(x=False, y=False)
    grid = getattr(plot, "_finder_grid", None)
    if settings.grid_visible and grid is None:
        grid = plot._finder_grid = SpectrumGrid(plot)
    if grid is not None:
        color = _color(settings.grid_color, "#8f969e")
        color.setAlphaF(max(0.0, min(1.0, settings.grid_alpha)))
        grid.pen = pg.mkPen(color, width=settings.grid_width)
        grid.setVisible(settings.grid_visible)
        if settings.grid_visible:
            grid.refresh()

    style = plot_style_from_view_settings(settings)
    for item in plot.plotItem.items:
        tag = canvas_item_tag(item)
        if tag is None or (tag.object_id or "").startswith("subtraction-"):
            continue
        if (tag.layer in {CanvasLayer.OBSERVED, CanvasLayer.PROCESSED, CanvasLayer.REFERENCE_PROFILE}
                or (tag.object_id or "").startswith("background-preview-")):
            pen = pg.mkPen(item.opts["pen"])
            if tag.layer == CanvasLayer.OBSERVED:
                pen.setWidthF(style.observed.width)
                if all_black:
                    pen.setColor(QColor("#000000"))
                elif tag.owner_id == getattr(plot, "_vibrational_spectrum_id", None) and settings.observed_color:
                    pen.setColor(_color(settings.observed_color, "#d81b60"))
                item.setVisible(settings.layer_observed_visible)
            elif tag.layer == CanvasLayer.PROCESSED:
                pen.setWidthF(style.calculated.width)
                pen.setColor(_color(settings.calculated_color, "#0b8043"))
                item.setVisible(settings.layer_total_profile_visible)
            elif tag.layer == CanvasLayer.REFERENCE_PROFILE:
                pen.setWidthF(style.reference.width)
                if not tag.exportable:
                    pen.setColor(_color(settings.reference_color, "#1a73e8"))
            else:
                pen.setColor(_color(settings.background_color, "#9aa0a6"))
                item.setVisible(settings.layer_background_visible)
            item.setPen(pen)
        elif tag.layer in {CanvasLayer.OBSERVED_BANDS, CanvasLayer.ASSIGNED_BANDS}:
            item.setSymbol(style.marker.symbol)
            item.setSize(style.marker.size)
