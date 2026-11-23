import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pyqtgraph as pg

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QSize
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QFileDialog

from vibrational_finder.plot_export.metadata import CanvasLayer, tag_canvas_item
from vibrational_finder.plot_export.options import PlotExportFormat, PlotExportOptions
from vibrational_finder.publication_export_controller import PublicationExportController
from vibrational_finder.ui.plot_export_dialog import PlotExportDialog
from vibrational_finder.ui.publication_export_actions import PublicationExportActionsMixin
from vibrational_finder.ui.vibrational_plot import create_vibrational_plot_widget


def count_column_pixels(image, point, radius, color):
    x, y = round(point.x()), round(point.y())
    return sum(
        color(image.pixelColor(x, row))
        for row in range(y - radius, y + radius + 1)
    )


class PublicationExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_high_dpi_raster_scales_stacked_traces_and_markers(self):
        plot = create_vibrational_plot_widget()
        self.addCleanup(plot.close)
        plot.resize(480, 320)
        plot.setTitle(None)
        plot.show()
        lower = tag_canvas_item(
            plot.plot([1, 9], [-1.5, -1.5], pen=pg.mkPen("#242424", width=2.0)),
            layer=CanvasLayer.OBSERVED,
        )
        tag_canvas_item(
            plot.plot([1, 9], [1, 1], pen=pg.mkPen("#284c85", width=2.0)),
            layer=CanvasLayer.OBSERVED,
        )
        marker = tag_canvas_item(
            pg.ScatterPlotItem([3], [-1.5], pen=None, brush="#902090", size=10),
            layer=CanvasLayer.ASSIGNED_BANDS,
        )
        plot.addItem(marker)
        cursor = tag_canvas_item(
            pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen("#159b10", width=2)),
            layer=CanvasLayer.CURSOR, exportable=False,
        )
        plot.addItem(cursor)
        plot.setRange(xRange=(0, 10), yRange=(-3, 3), padding=0)
        self.app.processEvents()
        ranges = plot.viewRange()
        view = plot.getViewBox()
        points = [plot.mapFromScene(view.mapViewToScene(QPointF(x, y)))
                  for x, y in ((4, -1.5), (4, 1), (3, -1.5), (4, 0))]
        colors = (
            lambda c: c.lightness() < 140,
            lambda c: c.blue() > c.red() + 25,
            lambda c: c.red() > c.green() + 30 and c.blue() > c.green() + 30,
        )
        controller = PublicationExportController()
        with TemporaryDirectory() as tmp:
            for format in (PlotExportFormat.PNG, PlotExportFormat.JPG):
                thicknesses = []
                for scale in (1, 4):
                    with self.subTest(format=format, scale=scale):
                        options = PlotExportOptions.for_canvas(
                            format, canvas_width_px=480, canvas_height_px=320, dpi=96 * scale,
                        )
                        path = controller.export(plot, options, Path(tmp) / f"figure-{scale}.{format}")
                        image = QImage(str(path))
                        self.assertEqual(image.size(), QSize(480 * scale, 320 * scale))
                        thicknesses.append([
                            count_column_pixels(image, point * scale, 10 * scale, color)
                            for point, color in zip(points[:3], colors, strict=True)
                        ])
                        self.assertEqual(
                            count_column_pixels(image, points[3] * scale, 10 * scale,
                                                lambda c: c.green() > c.red() + 30),
                            0, "The onscreen plot cursor must not be exported.",
                        )
                for normal, high_dpi in zip(*thicknesses, strict=True):
                    self.assertGreater(normal, 0, (format, thicknesses))
                    self.assertGreaterEqual(high_dpi, normal * 2.5, (format, thicknesses))
        self.assertEqual(plot.viewRange(), ranges)
        self.assertEqual(lower.opts["pen"].widthF(), 2.0)
        self.assertEqual(marker.opts["size"], 10)
        self.assertTrue(cursor.isVisible())

    def test_export_uses_raman_filename_and_preserves_jpeg_extension(self):
        actions = PublicationExportActionsMixin()
        with TemporaryDirectory() as tmp:
            actions.workspace_preferences = type(
                "Preferences", (), {"last_directory": lambda self, *_keys: tmp},
            )()
            target = str(Path(tmp) / "chosen.jpeg")
            with patch.object(QFileDialog, "getSaveFileName", return_value=(target, "")) as save:
                self.assertEqual(actions._choose_publication_export_path(PlotExportFormat.JPG), Path(target))
                self.assertEqual(Path(save.call_args.args[2]).name, "raman_figure.jpg")

    def test_first_export_has_a_fixed_publication_width(self):
        dialog = PlotExportDialog(QSize(1651, 840), None)
        self.assertEqual(dialog.options().width_mm, 180)
        self.assertLess(dialog.options().pixel_size()[0], 5000)
        dialog.one_to_one_button.click()
        self.assertAlmostEqual(dialog.options().width_mm, 1651 * 25.4 / 96, places=2)
