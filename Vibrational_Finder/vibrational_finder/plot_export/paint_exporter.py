from __future__ import annotations

import os
import uuid
from contextlib import ExitStack
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QByteArray, QFile, QIODevice, QMarginsF, QRectF, QSizeF, Qt
from PySide6.QtGui import QImage, QImageWriter, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen
from PySide6.QtWidgets import QAbstractGraphicsShapeItem

from .options import PlotExportFormat, PlotExportOptions
from .snapshot import FrozenCanvas
from .svg_exporter import LayeredSvgExporter


_FORMAT_SUFFIXES = {
    PlotExportFormat.SVG: {".svg"},
    PlotExportFormat.PDF: {".pdf"},
    PlotExportFormat.PNG: {".png"},
    PlotExportFormat.JPG: {".jpg", ".jpeg"},
}

_MAGIC = {
    PlotExportFormat.SVG: (b"<?xml", b"<svg"),
    PlotExportFormat.PDF: (b"%PDF-",),
    PlotExportFormat.PNG: (b"\x89PNG\r\n\x1a\n",),
    PlotExportFormat.JPG: (b"\xff\xd8\xff",),
}


def _paint_scene(snapshot: FrozenCanvas, painter: QPainter, target: QRectF) -> None:
    snapshot.export_items()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    # Keep scene containers visible; they hold the exportable axes and data.
    containers = (snapshot.plot_item, snapshot.view_box, snapshot.view_box.childGroup)
    with ExitStack() as restore:
        for entry in snapshot.items:
            if entry.visible and not entry.tag.exportable and entry.item not in containers:
                restore.callback(entry.item.setVisible, True)
                entry.item.hide()
        snapshot.scene.render(
            painter,
            target,
            snapshot.source_rect,
            Qt.AspectRatioMode.IgnoreAspectRatio,
        )


def _scale_cosmetic_pen(pen, scale: float) -> QPen | None:
    if not isinstance(pen, QPen) or not pen.isCosmetic() or pen.style() == Qt.PenStyle.NoPen:
        return None
    scaled = QPen(pen)
    scaled.setCosmetic(True)
    scaled.setWidthF((pen.widthF() or 1.0) * scale)
    return scaled


def _scale_raster_items(snapshot: FrozenCanvas, scale: float, restore: ExitStack) -> None:
    if abs(scale - 1.0) < 1e-6:
        return

    def scale_pen(setter, pen):
        if (scaled := _scale_cosmetic_pen(pen, scale)) is not None:
            restore.callback(setter, pen)
            setter(scaled)

    def scale_scatter(item):
        if item.opts["pxMode"]:
            restore.callback(item.setExportMode, False)
            scale_pen(item.setPen, item.opts["pen"])
            item.setExportMode(True, {"antialias": True, "resolutionScale": scale})

    # Qt cosmetic pens and pyqtgraph pixel symbols ignore the scene transform.
    # Scale them just for this paint pass, leaving layout and live settings alone.
    for entry in snapshot.export_items():
        item = entry.item
        if isinstance(item, pg.PlotDataItem):
            if item.scatter.isVisible():
                scale_scatter(item.scatter)
            item = item.curve
        if isinstance(item, pg.PlotCurveItem):
            scale_pen(item.setPen, item.opts["pen"])
            scale_pen(item.setShadowPen, item.opts["shadowPen"])
        elif isinstance(item, pg.ScatterPlotItem):
            scale_scatter(item)
        elif isinstance(item, (pg.AxisItem, QAbstractGraphicsShapeItem)):
            scale_pen(item.setPen, item.pen())
        elif isinstance(getattr(item, "pen", None), QPen):
            scale_pen(lambda pen, item=item: setattr(item, "pen", pen), item.pen)


def render_raster(snapshot: FrozenCanvas, options: PlotExportOptions) -> QImage:
    if options.format not in {PlotExportFormat.PNG, PlotExportFormat.JPG}:
        raise ValueError("render_raster requires PNG or JPG options")
    width, height = options.pixel_size()
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise MemoryError(f"Could not allocate {width}x{height} export image")
    # AxisItem creates tick text during painting. Use the live canvas's logical
    # DPI for that paint pass; the requested publication DPI is metadata and is
    # applied only after QPainter has finished.
    image.setDotsPerMeterX(round(float(snapshot.widget.logicalDpiX()) / 0.0254))
    image.setDotsPerMeterY(round(float(snapshot.widget.logicalDpiY()) / 0.0254))
    image.fill(snapshot.background.color())
    painter = QPainter(image)
    try:
        with ExitStack() as restore:
            scale = min(width / snapshot.source_rect.width(), height / snapshot.source_rect.height())
            _scale_raster_items(snapshot, scale, restore)
            _paint_scene(snapshot, painter, QRectF(0.0, 0.0, float(width), float(height)))
    finally:
        painter.end()
    dots_per_metre = round(options.dpi / 0.0254)
    image.setDotsPerMeterX(dots_per_metre)
    image.setDotsPerMeterY(dots_per_metre)
    return image


def _write_raster_image(image: QImage, options: PlotExportOptions, destination: Path) -> None:
    encoding = b"png" if options.format is PlotExportFormat.PNG else b"jpg"
    writer = QImageWriter(str(destination), QByteArray(encoding))
    if options.format is PlotExportFormat.JPG:
        writer.setQuality(options.jpeg_quality)
    if not writer.write(image):
        raise RuntimeError(f"Could not encode {options.format.value.upper()}: {writer.errorString()}")


def write_vector_pdf(snapshot: FrozenCanvas, options: PlotExportOptions, device: QIODevice) -> None:
    if options.format is not PlotExportFormat.PDF:
        raise ValueError("write_vector_pdf requires PDF options")
    snapshot.export_items()
    writer = QPdfWriter(device)
    # PDF geometry is vector and resolution-independent. Matching the live
    # device's logical DPI keeps dynamically painted axis text WYSIWYG.
    writer.setResolution(max(72, int(snapshot.widget.logicalDpiX())))
    page_size = QPageSize(
        QSizeF(options.width_mm, options.height_mm),
        QPageSize.Unit.Millimeter,
        "Publication figure",
        QPageSize.SizeMatchPolicy.ExactMatch,
    )
    writer.setPageSize(page_size)
    writer.setPageMargins(QMarginsF(0.0, 0.0, 0.0, 0.0), QPageLayout.Unit.Millimeter)
    painter = QPainter(writer)
    if not painter.isActive():
        raise RuntimeError("Could not start PDF painter")
    try:
        target = QRectF(0.0, 0.0, float(writer.width()), float(writer.height()))
        painter.fillRect(target, snapshot.background)
        _paint_scene(
            snapshot,
            painter,
            target,
        )
    finally:
        painter.end()
    del writer


def _write_svg(snapshot: FrozenCanvas, options: PlotExportOptions, destination: Path) -> None:
    data = LayeredSvgExporter().render(snapshot, options)
    file = QFile(str(destination))
    if not file.open(QIODevice.OpenModeFlag.WriteOnly | QIODevice.OpenModeFlag.Truncate):
        raise OSError(file.errorString())
    try:
        if file.write(data) != len(data) or not file.flush():
            raise OSError(file.errorString())
    finally:
        file.close()


def _write_pdf(snapshot: FrozenCanvas, options: PlotExportOptions, destination: Path) -> None:
    file = QFile(str(destination))
    if not file.open(QIODevice.OpenModeFlag.WriteOnly | QIODevice.OpenModeFlag.Truncate):
        raise OSError(file.errorString())
    try:
        write_vector_pdf(snapshot, options, file)
        if not file.flush():
            raise OSError(file.errorString())
    finally:
        file.close()


def _validate_output(destination: Path, format: PlotExportFormat) -> None:
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("Exporter produced an empty file")
    with destination.open("rb") as handle:
        prefix = handle.read(16).lstrip()
    if not any(prefix.startswith(magic) for magic in _MAGIC[format]):
        raise RuntimeError(f"Exporter produced an invalid {format.value.upper()} file")


def export_frozen_canvas(
    snapshot: FrozenCanvas,
    options: PlotExportOptions,
    destination: str | Path,
) -> None:
    destination = Path(destination)
    if destination.suffix.lower() not in _FORMAT_SUFFIXES[options.format]:
        allowed = ", ".join(sorted(_FORMAT_SUFFIXES[options.format]))
        raise ValueError(f"Destination suffix must be {allowed} for {options.format.value.upper()}")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        if options.format in {PlotExportFormat.PNG, PlotExportFormat.JPG}:
            _write_raster_image(render_raster(snapshot, options), options, temporary)
        elif options.format is PlotExportFormat.PDF:
            _write_pdf(snapshot, options, temporary)
        elif options.format is PlotExportFormat.SVG:
            _write_svg(snapshot, options, temporary)
        else:  # pragma: no cover - PlotExportFormat is exhaustive
            raise ValueError(f"Unsupported export format: {options.format}")
        _validate_output(temporary, options.format)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
