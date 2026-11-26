from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF

from vibrational_finder.plot_export.metadata import CanvasLayer, canvas_item_tag


_TRACE_LAYERS = {CanvasLayer.OBSERVED, CanvasLayer.PROCESSED, CanvasLayer.REFERENCE_PROFILE}


def visible_plot_traces(plot):
    """Visible curves in increasing x, preserving gaps in the plotted y values."""
    for item in plot.plotItem.items:
        tag = canvas_item_tag(item)
        if tag is None or not item.isVisible() or not hasattr(item, "getData"):
            continue
        if tag.layer not in _TRACE_LAYERS and not (tag.object_id or "").startswith("background-preview-"):
            continue
        x, y = item.getData()
        if x is None or y is None:
            continue
        x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
        finite = np.isfinite(x)
        if not np.any(finite & np.isfinite(y)):
            continue
        if not np.all(finite):
            x, y = x[finite], y[finite]
        if np.any(x[1:] < x[:-1]):
            order = np.argsort(x, kind="stable")
            x, y = x[order], y[order]
        yield item, tag, (x, y)


def trace_scene_intervals(view, traces, left: float, right: float) -> list[tuple[float, float]]:
    low, high = sorted(view.mapSceneToView(QPointF(x, 0)).x() for x in (left, right))
    blocked = []
    for grid, trace in traces:
        if high < grid[0] or low > grid[-1]:
            continue
        start, stop = np.searchsorted(grid, low), np.searchsorted(grid, high, side="right")
        segment = trace[max(0, start - 1):stop + 1]
        finite = segment[np.isfinite(segment)]
        if finite.size:
            a, b = (view.mapViewToScene(QPointF(0, float(y))).y() for y in (finite.min(), finite.max()))
            blocked.append((min(a, b), max(a, b)))
    return blocked
