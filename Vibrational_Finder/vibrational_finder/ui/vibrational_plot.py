from __future__ import annotations

from html import escape
from collections.abc import Mapping, Sequence
from dataclasses import replace
from math import ceil, log10

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QFont, QPainterPath
from PySide6.QtWidgets import QLayout, QVBoxLayout, QWidget

from vibrational_finder.core.models import SignalKind
from vibrational_finder.plot_export.metadata import CanvasLayer, canvas_item_tag, tag_canvas_item
from vibrational_finder.models import ObservedSpectrum, VibrationalMatchResult
from vibrational_finder.models import SpectralBand
from vibrational_finder.layers.bands import matched_reference_band_positions, reference_peak_label
from vibrational_finder.matching import MatchingOptions
from vibrational_finder.services.phase_deconvolution import BackgroundTrace, FittedBand
from vibrational_finder.ui.spectrum_legends import LegendSample
from vibrational_finder.units import spectral_x_to_nm


class SpectralAxisItem(pg.AxisItem):
    """Label a wavenumber-based canvas in either its native units or nm."""

    wavelength_context: tuple[SignalKind, float] | None = None

    def set_wavelength_context(self, context: tuple[SignalKind, float] | None) -> None:
        if self.wavelength_context != context:
            self.wavelength_context = context
            self.picture = None
            self.update()

    def display_values(self, values) -> np.ndarray:
        if self.wavelength_context is None:
            return np.asarray(values, dtype=float)
        kind, laser_nm = self.wavelength_context
        return spectral_x_to_nm(values, kind, laser_nm)

    def tickStrings(self, values, scale, spacing):
        if self.wavelength_context is None:
            return super().tickStrings(values, scale, spacing)
        wavelengths = self.display_values(values)
        deltas = np.abs(self.display_values(np.asarray(values, dtype=float) + abs(spacing)) - wavelengths)
        valid = deltas[np.isfinite(deltas) & (deltas > 0)]
        precision = min(8, max(2, ceil(-log10(float(valid.min()))) + 1)) if valid.size else 2
        return [f"{value:.{precision}f}".rstrip("0").rstrip(".") if np.isfinite(value) else ""
                for value in wavelengths]


class VibrationalViewBox(pg.ViewBox):
    def __init__(self) -> None:
        super().__init__(enableMenu=False)
        self.setMouseMode(pg.ViewBox.RectMode)
        self.setMouseEnabled(x=True, y=True)
        self.full_range_callback = None
        self._spectrum_x_limits: tuple[float, float] | None = None

    def set_spectrum_x_limits(self, axes: Sequence[Sequence[float]]) -> None:
        ranges = []
        for axis in axes:
            values = np.asarray(axis, dtype=float)
            finite = values[np.isfinite(values)]
            if finite.size:
                ranges.append((float(finite.min()), float(finite.max())))
        limits = None
        if ranges:
            low = min(first for first, _last in ranges)
            high = max(last for _first, last in ranges)
            span = high - low
            if span > 0:
                limits = (low - span, high + span)
        if self._spectrum_x_limits == limits:
            return
        self._spectrum_x_limits = limits
        self.setLimits(xMin=limits[0] if limits else None, xMax=limits[1] if limits else None)

    def wheelEvent(self, event, axis=None) -> None:
        delta = event.delta() if hasattr(event, "delta") else event.angleDelta().y()
        factor = 0.9 if delta > 0 else 1.1
        center = self.mapSceneToView(event.scenePos())
        self.scaleBy(x=factor, y=1.0, center=center)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        if self.full_range_callback is not None:
            self.full_range_callback()
        else:
            self.autoRange(padding=0.04)
        event.accept()


class VibrationalPlotWidget(pg.PlotWidget):
    secondaryClick = Signal(object)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.secondaryClick.emit(event.position().toPoint())
            event.accept()
            return
        super().mouseReleaseEvent(event)


def register_structural_plot_items(plot: pg.PlotWidget) -> None:
    plot_item = plot.plotItem
    view_box = plot_item.vb
    tag_canvas_item(
        plot_item,
        layer=CanvasLayer.BACKGROUND,
        object_id="plot-container",
        exportable=False,
    )
    tag_canvas_item(
        view_box,
        layer=CanvasLayer.BACKGROUND,
        object_id="view-box",
        exportable=False,
    )
    tag_canvas_item(
        view_box.childGroup,
        layer=CanvasLayer.BACKGROUND,
        object_id="view-box-children",
        exportable=False,
    )
    for axis_name in ("bottom", "left", "top", "right"):
        axis = plot.getAxis(axis_name)
        tag_canvas_item(
            axis,
            layer=CanvasLayer.AXES,
            object_id=f"axis-{axis_name}",
        )
        tag_canvas_item(
            axis.label,
            layer=CanvasLayer.AXES,
            object_id=f"axis-{axis_name}-label",
        )
    tag_canvas_item(
        plot_item.titleLabel,
        layer=CanvasLayer.AXES,
        object_id="plot-title",
    )

def create_vibrational_plot_widget() -> pg.PlotWidget:
    plot = VibrationalPlotWidget(
        viewBox=VibrationalViewBox(),
        axisItems={name: SpectralAxisItem(orientation=name) for name in ("bottom", "top")},
    )
    plot.setBackground("w")
    plot.showGrid(x=False, y=False)
    plot.setMenuEnabled(False)
    plot.setTitle("Raman Phase Finder: spectrum and candidate markers", color="#111111", size="13pt")
    for axis_name in ("bottom", "left"):
        axis = plot.getAxis(axis_name)
        axis.setPen(pg.mkPen("#111111", width=1.2))
        axis.setTextPen(pg.mkPen("#111111"))
        axis_font = QFont()
        axis_font.setPointSize(10)
        axis.setTickFont(axis_font)
        axis.setStyle(tickTextOffset=8)
    plot.setLabel("bottom", "Wavenumber", units="cm-1", color="#111111", **{"font-size": "12pt"})
    plot.setLabel("left", "Intensity", units="a.u.", color="#111111", **{"font-size": "12pt"})
    for name in ("bottom", "top", "left", "right"):
        axis = plot.getAxis(name)
        axis.setSIPrefixEnableRanges(())
        axis.enableAutoSIPrefix(False)
    register_structural_plot_items(plot)
    return plot


class SpectrumPlotCanvas(QWidget):
    """Fit the active plot to the available canvas, never to a data-unit ratio."""

    def __init__(self, plot: QWidget) -> None:
        super().__init__()
        self.plot = plot
        self._shown_plot = plot
        self._aspect: float | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.addWidget(plot, 1)

    def show_preview(self, preview: QWidget) -> None:
        self.layout().insertWidget(0, preview, 1)
        self.plot.hide()
        self._shown_plot = preview
        preview.show()
        self._fit_widget()

    def close_preview(self, preview: QWidget) -> None:
        self.layout().removeWidget(preview)
        preview.hide()
        preview.deleteLater()
        self._shown_plot = self.plot
        self.plot.show()
        self._fit_widget()

    def set_aspect(self, aspect: float | None) -> None:
        self._aspect = aspect
        self._fit_widget()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_widget()

    def _fit_widget(self) -> None:
        widget = self._shown_plot
        if self._aspect is None:
            widget.setMinimumSize(0, 0)
            widget.setMaximumSize(16777215, 16777215)
            self.layout().setAlignment(widget, Qt.AlignmentFlag(0))
            return
        width, height = max(1, self.width()), max(1, self.height())
        target_width = min(width, max(1, round(height * self._aspect)))
        target_height = min(height, max(1, round(target_width / self._aspect)))
        self.layout().setAlignment(widget, Qt.AlignmentFlag.AlignCenter)
        widget.setFixedSize(target_width, target_height)


_BAND_ANNOTATION_LAYERS = {CanvasLayer.MODE_LABELS, CanvasLayer.ASSIGNMENT_LABELS}


def _add_band_annotations(
    plot, band, x, y, color, owner_id, object_id, *,
    mode=False, assignment=False, position=False, below=False, exportable=True,
) -> tuple[LegendSample, ...]:
    labels = (
        (CanvasLayer.PEAK_LABELS, f"{band.position:.1f}" if position else ""),
        (CanvasLayer.MODE_LABELS, reference_peak_label(band) if mode else ""),
        (CanvasLayer.ASSIGNMENT_LABELS,
         reference_peak_label(band, show_mode=False, show_assignment=True) if assignment else ""),
    )
    labels = tuple((layer, text) for layer, text in labels if text)
    for row, (layer, text) in enumerate(labels):
        anchor_y = -0.2 - row if below else 1.2 + row
        label = pg.TextItem(text, color=color, anchor=(0.5, anchor_y))
        label.textItem.document().setDocumentMargin(1)
        label._band_annotation_anchor = anchor_y
        label.setPos(float(x), float(y))
        tag_canvas_item(label, layer=layer, owner_id=owner_id, object_id=f"{object_id}-{layer.value}",
                        exportable=exportable)
        plot.addItem(label, ignoreBounds=True)
    kinds = {CanvasLayer.PEAK_LABELS: "position", CanvasLayer.MODE_LABELS: "mode",
             CanvasLayer.ASSIGNMENT_LABELS: "assignment"}
    return tuple(LegendSample(kinds[layer], text) for layer, text in labels)


def _record_legend_samples(samples_by_phase, key, *entries: LegendSample) -> None:
    if entries:
        samples = samples_by_phase.setdefault(key, {})
        for entry in entries:
            samples.setdefault(entry.kind, entry)


def _legend_samples(samples_by_phase) -> dict[str, tuple[LegendSample, ...]]:
    order = ("line", "dash", "dot", "tick", "marker", "mode", "assignment", "position")
    return {key: tuple(samples[kind] for kind in order if kind in samples)
            for key, samples in samples_by_phase.items()}


def layout_band_annotations(plot) -> None:
    """Pack labels in screen space while their scientific x/y anchors stay fixed."""
    view = plot.getViewBox()
    bounds = view.sceneBoundingRect().adjusted(2, 2, -2, -2)
    if bounds.isEmpty():
        return
    occupied = []
    labels = []
    priorities = {CanvasLayer.PEAK_LABELS: 0, CanvasLayer.MODE_LABELS: 1, CanvasLayer.ASSIGNMENT_LABELS: 2}
    for item in plot.plotItem.items:
        tag = canvas_item_tag(item)
        if tag is None:
            continue
        if tag.layer == CanvasLayer.LEGEND and item.isVisible():
            item.updateTransform()
            occupied.append(item.sceneBoundingRect())
        elif hasattr(item, "_band_annotation_anchor"):
            labels.append((priorities[tag.layer], not tag.exportable, item))
    for _, _, item in sorted(labels, key=lambda entry: (*entry[:2], entry[2].pos().x())):
        point = view.mapViewToScene(item.pos())
        anchor = item._band_annotation_anchor
        width, height = item.textItem.boundingRect().size().toTuple()
        placed = False
        if bounds.left() <= point.x() <= bounds.right() and bounds.top() <= point.y() <= bounds.bottom():
            for row in range(4):
                candidate = anchor + row * (1 if anchor > 0 else -1)
                box = QRectF(point.x() - width / 2, point.y() - candidate * height, width, height)
                if bounds.contains(box) and not any(previous.intersects(box) for previous in occupied):
                    item.setAnchor((.5, candidate))
                    occupied.append(box.adjusted(-2, -1, 2, 1))
                    placed = True
                    break
        if item.isVisible() != placed:
            item.setVisible(placed)


def _measured_band_points(spectrum, bands, offset=0.0):
    x, y = np.asarray(spectrum.x, dtype=float), np.asarray(spectrum.y, dtype=float)
    finite = np.isfinite(x) & np.isfinite(y)
    if not np.any(finite):
        return
    x, y = x[finite], y[finite]
    top = float(y.max())
    span = float(np.ptp(y))
    for i, band in enumerate(bands):
        if not np.isfinite(band.position) or not x.min() <= band.position <= x.max():
            continue
        level = float(y[np.argmin(abs(x - band.position))])
        yield i, band, level + offset, span > 0 and level > top - span * .12


def show_subtraction_layers(
    plot, spectrum: ObservedSpectrum, phases: Mapping[str, tuple[str, str]], bands: Sequence[FittedBand],
    settings: Mapping, *, offset: float, normalization: tuple[float, float], exportable: bool,
    background: BackgroundTrace | None = None,
    experiment: tuple[np.ndarray, np.ndarray] | None = None,
    residual_visible: bool = True,
) -> dict[str, tuple[LegendSample, ...]]:
    """Plot optional fit components and the measured positions without modifying the spectrum."""
    x, y = np.asarray(spectrum.x), np.asarray(spectrum.y)
    finite = np.isfinite(x) & np.isfinite(y)
    if not np.any(finite):
        return {}
    x, y = x[finite], y[finite]
    order = np.argsort(x)
    x, y = x[order], y[order]
    bottom = float(np.min(y)) + offset
    span = float(np.ptp(y)) or 1.0
    origin, scale = normalization
    if background is not None:
        background_x, background_y = background
        background_at_samples = np.interp(x, background_x, background_y)
        if settings.get("subtraction_background_visible", True):
            baseline_item = pg.PlotCurveItem(
                x, (background_at_samples - origin) * scale + offset,
                pen=pg.mkPen("#778392", width=1.3, style=Qt.PenStyle.DotLine),
            )
            tag_canvas_item(baseline_item, layer=CanvasLayer.PROCESSED, owner_id=spectrum.id,
                            object_id=f"subtraction-background-{spectrum.id}", exportable=exportable)
            plot.addItem(baseline_item)
    phase_settings = settings.get("subtraction_phase_layers", {})
    marker_groups = {}
    phase_rows = {}
    profiles = {}
    labels = []
    label_guides = {}
    samples_by_phase = {}
    for i, band in enumerate(bands):
        if not x[0] <= band.position <= x[-1]:
            continue
        key = band.phase_key or ""
        options = phase_settings.get(key, {})
        if not options.get("visible", True):
            continue
        _name, color = phases.get(key, ("", "#515661"))
        profile_visible = bool(key and options.get("profile", False))
        gaussian_visible = bool(options.get("gaussians", False))
        component_x, component_y = np.asarray(band.curve_x), np.asarray(band.curve_y)
        if profile_visible:
            values = profiles.setdefault(key, np.zeros(x.size))
            values += np.interp(x, component_x, component_y, left=0, right=0)
            _record_legend_samples(samples_by_phase, key, LegendSample("line"))
        if gaussian_visible:
            in_range = (component_x >= x[0]) & (component_x <= x[-1])
            baseline = np.interp(component_x, background_x, background_y) if background is not None else band.baseline
            gaussian_height = (component_y + baseline - origin) * scale + offset
            curve = pg.PlotCurveItem(
                component_x[in_range], gaussian_height[in_range],
                pen=pg.mkPen(color, width=1.25, style=Qt.PenStyle.DashLine),
            )
            tag_canvas_item(curve, layer=CanvasLayer.REFERENCE_PROFILE, owner_id=spectrum.id,
                            object_id=f"subtraction-gaussian-{i}", exportable=exportable)
            plot.addItem(curve)
            _record_legend_samples(samples_by_phase, key, LegendSample("dash"))
        if band.removed:
            row = phase_rows.setdefault(key, len(phase_rows))
            height = bottom - (.035 + .035 * row) * span
            markers_on = settings.get("subtraction_removed_markers", True)
        else:
            height = float(np.interp(band.position, x, y)) + offset
            markers_on = settings.get("subtraction_remaining_markers", True)
        markers_visible = markers_on and options.get("markers", True)
        if markers_visible:
            marker_groups.setdefault((key, band.removed), (color, [], []))
            _, positions, heights = marker_groups[key, band.removed]
            positions.append(band.position)
            heights.append(height)
            _record_legend_samples(samples_by_phase, key, LegendSample("tick" if band.removed else "marker"))
        mode = bool(options.get("modes", False))
        assignment = bool(options.get("assignments", False))
        position = bool(options.get("positions", False) or (mode or assignment) and not (
            mode and band.mode or assignment and band.assignment
        ))
        if not (mode or assignment or position):
            continue
        # A lower reference tick is not a peak. Anchor labels independently
        # to the local modeled lobe or to the corresponding visible trace.
        if (profile_visible or gaussian_visible) and component_x.size:
            valid = np.flatnonzero((component_x >= x[0]) & (component_x <= x[-1]))
            peak = valid[np.argmax(np.abs(component_y[valid]))] if valid.size else None
        else:
            peak = None
        if profile_visible and peak is not None:
            labels.append((key, component_x[peak], None, i, band, color, mode, assignment, position))
        elif experiment is not None:
            level = (float(np.interp(band.position, *experiment)) - origin) * scale + offset
            labels.append((key, band.position, level, i, band, color, mode, assignment, position))
        elif gaussian_visible and peak is not None:
            labels.append((key, component_x[peak], gaussian_height[peak], i, band, color, mode, assignment, position))
        elif not key and not band.removed and residual_visible:
            labels.append((key, band.position, height, i, band, color, mode, assignment, position))
        else:
            residual_y = float(np.interp(band.position, x, y)) + offset
            start, tip = residual_y + .025 * span, residual_y + .08 * span
            positions, heights = label_guides.setdefault(color, ([], []))
            positions.extend((band.position, band.position))
            heights.extend((start, tip))
            _record_legend_samples(samples_by_phase, key, LegendSample("tick"))
            labels.append((key, band.position, tip, i, band, color, mode, assignment, position))
    for key, values in profiles.items():
        _name, color = phases[key]
        baseline = float(np.max(y)) + offset if np.min(values) < 0 else bottom
        if background is not None:
            baseline = (background_at_samples - origin) * scale + offset
        profiles[key] = baseline + values * scale
        curve = pg.PlotCurveItem(x, profiles[key], pen=pg.mkPen(color, width=1.6))
        tag_canvas_item(curve, layer=CanvasLayer.REFERENCE_PROFILE, owner_id=spectrum.id,
                        object_id=f"subtraction-profile-{key}", exportable=exportable)
        plot.addItem(curve)
    for key, label_x, level, i, band, color, mode, assignment, position in labels:
        if level is None:
            level = float(np.interp(label_x, x, profiles[key]))
        annotations = _add_band_annotations(
            plot, SpectralBand(position=band.position, intensity=1, mode=band.mode, assignment=band.assignment),
            label_x, level, color, spectrum.id, f"subtraction-label-{i}",
            mode=mode, assignment=assignment, position=position, exportable=exportable,
        )
        _record_legend_samples(samples_by_phase, key, *annotations)
    tick = QPainterPath()
    tick.moveTo(0, -.5)
    tick.lineTo(0, .5)
    for i, (color, (positions, heights)) in enumerate(label_guides.items()):
        guide = pg.PlotCurveItem(positions, heights, connect="pairs", pen=pg.mkPen(color, width=1.35))
        tag_canvas_item(guide, layer=CanvasLayer.REFERENCE_TICKS, owner_id=spectrum.id,
                        object_id=f"subtraction-anchor-tick-{i}", exportable=exportable)
        plot.addItem(guide)
    for i, ((_key, removed), (color, positions, heights)) in enumerate(marker_groups.items()):
        markers = pg.ScatterPlotItem(
            x=positions, y=heights, symbol=tick if removed else "o", size=9 if removed else 7,
            pen=pg.mkPen(color, width=1.6), brush=None if removed else pg.mkBrush(color),
        )
        tag_canvas_item(markers, layer=CanvasLayer.REFERENCE_TICKS if removed else CanvasLayer.ASSIGNED_BANDS,
                        owner_id=spectrum.id, object_id=f"subtraction-marker-{i}", exportable=exportable)
        plot.addItem(markers)
    return _legend_samples(samples_by_phase)


def show_subtraction_experiment(plot, spectrum, fit, normalization, *, experiment_visible, residual_visible):
    prefix = "preview" if fit is not None else "observed"
    residual = next((item for item in plot.listDataItems() if (tag := canvas_item_tag(item)) is not None
                     and tag.object_id == f"{prefix}-{spectrum.id}"), None)
    if residual is not None:
        pen = pg.mkPen(residual.opts["pen"])
        pen.setStyle(Qt.PenStyle.DashLine)
        residual.setPen(pen)
        residual.setVisible(residual_visible)
        tag_canvas_item(residual, layer=CanvasLayer.PROCESSED if fit is not None else CanvasLayer.OBSERVED,
                        owner_id=spectrum.id, object_id=f"subtraction-residual-{spectrum.id}", exportable=fit is None)
    if fit is not None and experiment_visible:
        origin, scale = normalization
        experiment = plot.plot(fit.x, (fit.measured - origin) * scale,
                               pen=pg.mkPen("#47434b", width=1.35))
        experiment.setZValue(-1)
        tag_canvas_item(experiment, layer=CanvasLayer.OBSERVED, owner_id=spectrum.id,
                        object_id=f"subtraction-experiment-{spectrum.id}", exportable=False)


def show_observed_spectrum(
    plot: pg.PlotWidget,
    spectrum: ObservedSpectrum | None,
    *,
    preview: bool = False,
    background_y: list[float] | None = None,
    bands: list[SpectralBand] | None = None,
    selected_band_index: int | None = None,
    show_band_labels: bool = True,
    show_mode_labels: bool = False,
    show_assignment_labels: bool = False,
    on_band_selected=None,
    preserve_view: bool = False,
    preserve_x_range: bool = False,
    overlays: tuple[tuple[ObservedSpectrum, float], ...] = (),
    overlay_bands: Mapping[str, Sequence[SpectralBand]] | None = None,
    all_observed_black: bool = False,
) -> None:
    """Replace the plotted trace without changing the source spectrum."""
    old_range = plot.viewRange() if preserve_view or preserve_x_range else None
    view = plot.getViewBox()
    if isinstance(view, VibrationalViewBox):
        view.set_spectrum_x_limits((spectrum.x, *(other.x for other, _offset in overlays)) if spectrum else ())
    plot.clear()
    if spectrum is None:
        plot._vibrational_spectrum_id = None
        return
    plot._vibrational_spectrum_id = spectrum.id
    color = "#d81b60" if spectrum.kind.value == "raman" else "#008ca8"
    trace_color = "#000000" if all_observed_black else color
    trace = plot.plot(spectrum.x, spectrum.y, pen=pg.mkPen(trace_color, width=1.6), name=spectrum.name)
    tag_canvas_item(
        trace,
        layer=CanvasLayer.PROCESSED if preview else CanvasLayer.OBSERVED,
        owner_id=spectrum.id,
        object_id=f"{'preview' if preview else 'observed'}-{spectrum.id}",
        exportable=not preview,
    )
    overlay_colors = ("#176e99", "#986323", "#7451a1", "#197664", "#ad4736")
    for index, (other, offset) in enumerate(overlays):
        overlay_color = overlay_colors[index % len(overlay_colors)]
        trace_color = "#000000" if all_observed_black else overlay_color
        item = plot.plot(other.x, (np.asarray(other.y) + offset).tolist(), name=other.name,
                         pen=pg.mkPen(trace_color, width=1.6))
        tag_canvas_item(item, layer=CanvasLayer.OBSERVED, owner_id=other.id, object_id=f"observed-{other.id}")
        if overlay_bands and (show_mode_labels or show_assignment_labels):
            for row, band, height, below in _measured_band_points(other, overlay_bands.get(other.id, ()), offset):
                _add_band_annotations(plot, band, band.position, height, overlay_color, other.id,
                                      f"observed-band-label-{other.id}-{row}", mode=show_mode_labels,
                                      assignment=show_assignment_labels, below=below)
    if background_y is not None and len(background_y) == len(spectrum.x):
        background = plot.plot(
            spectrum.x,
            background_y,
            pen=pg.mkPen("#7e8792", width=1.2, style=Qt.PenStyle.DashLine),
            name="Background preview",
        )
        tag_canvas_item(
            background,
            layer=CanvasLayer.BACKGROUND,
            object_id=f"background-preview-{spectrum.id}",
            exportable=False,
        )
    for index, band, marker_y, below in _measured_band_points(spectrum, bands or ()):
        marker = pg.ScatterPlotItem(
            [band.position],
            [marker_y],
            symbol="o",
            size=11 if index == selected_band_index else 8,
            pen=pg.mkPen("#b71c1c", width=1.2),
            brush=pg.mkBrush("#ef5350"),
            hoverable=True,
            useCache=False,
        )
        callback = (lambda band_index=index: on_band_selected(band_index)) if callable(on_band_selected) else lambda: None
        marker._vibrational_band_index = index
        marker._vibrational_select_callback = callback
        marker.sigClicked.connect(lambda *_args, select=callback: select())
        tag_canvas_item(
            marker,
            layer=CanvasLayer.OBSERVED_BANDS,
            owner_id=spectrum.id,
            object_id=f"observed-band-{spectrum.id}-{index}",
        )
        plot.addItem(marker)
        _add_band_annotations(
            plot, band, band.position, marker_y, "#b71c1c", spectrum.id,
            f"observed-band-label-{spectrum.id}-{index}", mode=show_mode_labels,
            assignment=show_assignment_labels, position=show_band_labels or index == selected_band_index,
            below=below, exportable=not preview,
        )
    plot.setTitle(spectrum.name, color="#111111", size="13pt")
    plot.setLabel("bottom", "Raman shift" if spectrum.kind.value == "raman" else "Wavenumber", units="cm-1")
    plot.setLabel("left", "Intensity", units="a.u.")
    if preserve_view and old_range is not None:
        plot.setRange(xRange=old_range[0], yRange=old_range[1], padding=0)
    else:
        plot.autoRange(padding=0.04)
        if old_range is not None and spectrum.x:
            left, right = old_range[0]
            if max(left, min(spectrum.x)) < min(right, max(spectrum.x)):
                plot.setXRange(left, right, padding=0)


def show_reference_layers(
    plot: pg.PlotWidget,
    results: list[VibrationalMatchResult],
    visible_keys: set[str],
    colors: dict[str, str],
    *,
    residual_bands: list[SpectralBand] | None = None,
    observed: ObservedSpectrum | None = None,
    preview_key: str | None = None,
    preserve_view: bool = True,
    show_profiles: bool = True,
    show_preview_profile: bool = False,
    show_bands: bool = True,
    show_peak_markers: bool = False,
    show_reference_ticks: bool = False,
    show_mode_labels: bool = False,
    show_assignment_labels: bool = False,
    display_offset: float = 0.0,
    clear_existing: bool = True,
    fit_markers: bool = False,
    observed_bands: Sequence[SpectralBand] = (),
    relative_reference_profiles: bool = False,
) -> dict[str, tuple[LegendSample, ...]]:
    old_range = plot.viewRange() if preserve_view else None
    removable_layers = {
        CanvasLayer.REFERENCE_PROFILE, CanvasLayer.REFERENCE_BANDS, CanvasLayer.REFERENCE_TICKS,
        CanvasLayer.ASSIGNED_BANDS, CanvasLayer.UNASSIGNED_BANDS,
    }
    if clear_existing:
        for item in list(plot.plotItem.items):
            tag = getattr(item, "_vibrational_export_tag", None)
            if tag is not None and (
                tag.layer in removable_layers
                or (tag.layer in _BAND_ANNOTATION_LAYERS and (tag.object_id or "").startswith("reference-band-label-"))
            ):
                plot.removeItem(item)
    measured = _finite_sorted_trace(observed)
    samples_by_phase = {}
    lane_index = 0
    for result in results:
        key = result.candidate.key
        if key not in visible_keys:
            continue
        color = colors.get(key, "#1a73e8")
        is_preview = key == preview_key
        visual_key = f"{observed.id}-{key}" if observed is not None else key
        reference = result.aligned_reference or result.candidate.reference
        profile_visible = show_preview_profile if is_preview else show_profiles
        if profile_visible and reference is not None and reference.x and reference.y:
            y = np.asarray(_reference_display_y(reference.y, observed, profile=True), dtype=float)
            if relative_reference_profiles and observed is None:
                finite_y = y[np.isfinite(y)]
                if finite_y.size:
                    y = (y - finite_y.min()) / (float(np.ptp(finite_y)) or 1.0)
            y = y + display_offset
            curve = pg.PlotCurveItem(
                reference.x, y,
                pen=pg.mkPen(color, width=1.4, style=Qt.PenStyle.DashLine if is_preview else Qt.PenStyle.SolidLine),
            )
            tag_canvas_item(
                curve,
                layer=CanvasLayer.REFERENCE_PROFILE,
                owner_id=key,
                object_id=f"reference-profile-{visual_key}",
                exportable=not is_preview,
            )
            plot.addItem(curve, ignoreBounds=preserve_view)
            _record_legend_samples(samples_by_phase, key, LegendSample("dash" if is_preview else "line"))
        bands = result.reference_bands or (
            list(result.candidate.band_set.bands) if result.candidate.band_set is not None else []
        )
        if bands and (show_bands or show_peak_markers or show_reference_ticks
                      or show_mode_labels or show_assignment_labels):
            reference_positions = np.asarray([band.position for band in bands], dtype=float)
            nominal_preview = measured is None or (
                is_preview and result.matched_peak_pairs is None and not result.observed_bands and not observed_bands
            )
            positions = reference_positions + result.score.x_shift if nominal_preview else _matched_peak_x(
                result, bands, observed_bands,
            )
            valid = np.isfinite(positions)
            if measured is not None:
                grid, trace = measured
                valid &= (positions >= grid[0]) & (positions <= grid[-1])
                span = float(np.ptp(trace)) or float(np.max(np.abs(trace))) or 1.0
                spectrum_base = float(trace.min()) + display_offset
                first_lane_top = spectrum_base - span * .045
                drawn_x = positions[valid]
                peak_y = np.interp(drawn_x, grid, trace)
                heights = peak_y + display_offset
                if show_bands:
                    # Start just above the nearest comparison row, even when
                    # that row is hidden. Browse previews have no comparison row.
                    start = spectrum_base if is_preview else first_lane_top + span * .010
                    bases = np.full_like(heights, start)
                if show_mode_labels or show_assignment_labels:
                    label_y = _nearby_trace_tops(grid, trace, drawn_x) + display_offset + span * .055
                if show_reference_ticks and not is_preview:
                    top = first_lane_top - span * lane_index * .059
                    height = span * .037
                    bottom, middle = top - height, top - height / 2
                    lane_x, lane_y = [], []
                    for index, nominal in enumerate(reference_positions):
                        if np.isfinite(nominal) and (grid[0] <= nominal <= grid[-1] or valid[index]):
                            lane_x.extend((nominal, nominal))
                            lane_y.extend((bottom, middle))
                            if valid[index]:
                                lane_x.extend((positions[index], positions[index]))
                                lane_y.extend((middle, top))
                    if lane_x:
                        _add_reference_lines(
                            plot, np.asarray(lane_x), np.asarray(lane_y), color, key,
                            f"reference-ticks-{visual_key}", layer=CanvasLayer.REFERENCE_TICKS,
                            ignore_bounds=not fit_markers,
                        )
                        _record_legend_samples(samples_by_phase, key, LegendSample("tick"))
                        lane_index += 1
            else:
                intensities = np.asarray([band.intensity for band in bands], dtype=float)
                valid &= np.isfinite(intensities)
                strengths = np.clip(intensities[valid], 0.0, None)
                maximum = float(np.max(strengths, initial=0))
                heights = np.maximum(strengths / maximum, .025) if maximum > 0 else np.ones_like(strengths)
                heights += display_offset
                bases = np.full_like(heights, display_offset)
                label_y = heights if show_bands else bases
            if not np.any(valid):
                continue
            positions = positions[valid]
            if show_bands:
                _add_reference_lines(
                    plot, np.repeat(positions, 2), np.column_stack((bases, heights)).ravel(), color, key,
                    f"reference-bands-{visual_key}", exportable=not is_preview, ignore_bounds=not fit_markers,
                )
                _record_legend_samples(samples_by_phase, key, LegendSample("tick"))
            if show_peak_markers and (not nominal_preview or (measured is None and not is_preview)):
                dots = pg.ScatterPlotItem(
                    positions, heights, symbol="o", size=8, brush=pg.mkBrush(color),
                    pen=pg.mkPen("#ffffff", width=1), useCache=False,
                )
                dots.setZValue(3)
                tag_canvas_item(
                    dots, layer=CanvasLayer.ASSIGNED_BANDS, owner_id=key,
                    object_id=f"reference-markers-{visual_key}", exportable=not is_preview,
                )
                plot.addItem(dots, ignoreBounds=measured is not None or preserve_view)
                _record_legend_samples(samples_by_phase, key, LegendSample("marker"))
            if (show_mode_labels or show_assignment_labels) and (not nominal_preview or measured is None):
                for index, x, y in zip(np.flatnonzero(valid), positions, label_y, strict=True):
                    annotations = _add_band_annotations(
                        plot, bands[index], x, y, color, key, f"reference-band-label-{visual_key}-{index}",
                        mode=show_mode_labels, assignment=show_assignment_labels,
                        exportable=not is_preview,
                    )
                    _record_legend_samples(samples_by_phase, key, *annotations)
    residual = list(residual_bands or [])
    if residual:
        owner = str(getattr(plot, "_vibrational_spectrum_id", None) or "residual")
        marker = pg.ScatterPlotItem(
            [float(band.position) for band in residual],
            [float(band.intensity) for band in residual],
            symbol="x",
            size=10,
            pen=pg.mkPen("#f57c00", width=1.5),
            useCache=False,
        )
        tag_canvas_item(
            marker,
            layer=CanvasLayer.UNASSIGNED_BANDS,
            owner_id=owner,
            object_id=f"residual-bands-{owner}",
        )
        plot.addItem(marker, ignoreBounds=True)
    if old_range is not None:
        plot.setRange(xRange=old_range[0], yRange=old_range[1], padding=0)
    elif visible_keys:
        plot.autoRange(padding=0.04)
    return _legend_samples(samples_by_phase)


def _add_reference_lines(plot, x, y, color, owner, object_id, *, layer=CanvasLayer.REFERENCE_BANDS,
                         exportable=True, ignore_bounds=False) -> None:
    item = pg.PlotCurveItem(x, y, connect="pairs", pen=pg.mkPen(color, width=1.3))
    tag_canvas_item(item, layer=layer, owner_id=owner, object_id=object_id, exportable=exportable)
    plot.addItem(item, ignoreBounds=ignore_bounds)


def _matched_peak_x(result, bands, sample_bands) -> np.ndarray:
    positions = np.full(len(bands), np.nan)
    if result.matched_peak_pairs is not None:
        for index, observed_x in result.matched_peak_pairs:
            if 0 <= index < len(bands):
                positions[index] = observed_x
        return positions
    # Older projects did not save pairs; use their detected bands and the legacy
    # one-to-one association, never a nominal position as a measured peak.
    if not result.observed_bands and sample_bands:
        result = replace(result, observed_bands=list(sample_bands))
    matched = matched_reference_band_positions(result, bands, result.score.x_shift, MatchingOptions().tolerance_cm1)
    return np.asarray([matched.get(id(band), np.nan) for band in bands], dtype=float)


def _finite_sorted_trace(observed):
    if observed is None:
        return None
    x, y = np.asarray(observed.x, dtype=float), np.asarray(observed.y, dtype=float)
    finite = np.isfinite(x) & np.isfinite(y)
    if not np.any(finite):
        return None
    x, y = x[finite], y[finite]
    order = np.argsort(x, kind="stable")
    return x[order], y[order]


def _nearby_trace_tops(x, y, positions):
    # A nearby maximum gives the mark clearance from a slightly displaced
    # peak without moving the reference wavenumber or asserting a match.
    tops = np.interp(positions, x, y)
    for index, (left, right) in enumerate(zip(np.searchsorted(x, positions - 4.0),
                                             np.searchsorted(x, positions + 4.0, side="right"), strict=True)):
        if right > left:
            tops[index] = max(tops[index], y[left:right].max())
    return tops


def _reference_display_y(values: list[float], observed: ObservedSpectrum | None, *, profile: bool = False):
    if observed is None or not observed.y:
        return values
    y = np.asarray(values, dtype=float)
    measured = np.asarray(observed.y, dtype=float)
    measured = measured[np.isfinite(measured)]
    finite = y[np.isfinite(y)]
    if not len(measured) or not len(finite):
        return values
    bottom = np.min(measured)
    span = float(np.ptp(measured)) or 1.0
    if profile:
        relative = (y - np.min(finite)) / (float(np.ptp(finite)) or 1.0)
    else:
        relative = np.clip(y / max(1.0, float(np.max(finite))), 0.0, 1.0)
    return bottom + span * relative
