from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import numpy as np
from scipy.signal import find_peaks

from vibrational_finder.models import ObservedSpectrum, SpectralBand
from vibrational_finder.processing_state import ProcessingOperation
from vibrational_finder.services.preprocessing_service import estimate_background
from vibrational_finder.services.spectrum_processing import build_phase_subtraction_operation


def prepare_measured_phase_profile(
    reference: ObservedSpectrum, *, remove_background: bool = True,
) -> ObservedSpectrum:
    """Prepare a copy; never take background or intensity units from the card."""
    x, y = np.asarray(reference.x, dtype=float), np.asarray(reference.y, dtype=float)
    if x.ndim != 1 or x.shape != y.shape or x.size < 2 or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("The measured phase must contain finite paired spectral points.")
    order = np.argsort(x)
    x, y = x[order], y[order]
    if np.any(np.diff(x) <= 0):
        raise ValueError("The measured phase contains duplicate spectral coordinates.")
    if remove_background:
        y = np.maximum(y - estimate_background(x, y, method="arpls"), 0.0)
    return replace(reference, x=x.tolist(), y=y.tolist())


def _resolved_peaks(y: np.ndarray):
    prominence = max(.025 * float(np.ptp(y)), 4 * float(np.median(np.abs(np.diff(y)))))
    return find_peaks(y, prominence=prominence, width=1.0)


def _diagnostic_windows(
    spectrum: ObservedSpectrum, reference: ObservedSpectrum,
    phase_bands: Sequence[SpectralBand], other_bands: Sequence[SpectralBand],
) -> tuple[tuple[float, float], ...]:
    x, y = np.asarray(reference.x), np.asarray(reference.y)
    mixture_x = np.asarray(spectrum.x)
    if x.size < 5 or not mixture_x.size:
        return ()
    lo, hi = max(x[0], mixture_x.min()), min(x[-1], mixture_x.max())
    # A line from the card is eligible only if there is a nearby peak in
    # the separately measured profile.
    peaks, _ = _resolved_peaks(y)
    positions = x[peaks]
    other = np.asarray([band.position for band in other_bands if np.isfinite(band.position)])
    windows: list[tuple[float, float]] = []
    for band in phase_bands:
        if not np.isfinite(band.position) or not positions.size:
            continue
        radius = float(np.clip(2 * band.width, 8., 24.)) if np.isfinite(band.width) and band.width > 0 else 12.
        center = float(positions[np.argmin(np.abs(positions - band.position))])
        if abs(center - band.position) > 8 or not lo < center < hi:
            continue
        if np.any(np.abs(other - center) <= radius + 4):
            continue
        left, right = float(max(lo, center - radius)), float(min(hi, center + radius))
        if np.count_nonzero((mixture_x >= left) & (mixture_x <= right)) >= 5:
            windows.append((left, right))
    merged: list[tuple[float, float]] = []
    for left, right in sorted(windows):
        if merged and left <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(right, merged[-1][1])
        else:
            merged.append((left, right))
    return tuple(merged)


def _matching_windows(
    spectrum: ObservedSpectrum, reference: ObservedSpectrum,
) -> tuple[tuple[float, float], ...]:
    trace = prepare_measured_phase_profile(spectrum, remove_background=False)
    x, y = np.asarray(trace.x), np.asarray(trace.y)
    rx, ry = np.asarray(reference.x), np.asarray(reference.y)
    if min(x.size, rx.size) < 5:
        return ()
    lo, hi = max(x[0], rx[0]), min(x[-1], rx[-1])
    peaks, properties = _resolved_peaks(ry)
    target_peaks, _ = _resolved_peaks(y)
    target_centers = x[target_peaks]
    positions = np.arange(rx.size)
    widths = np.interp(properties["right_ips"], positions, rx) - np.interp(properties["left_ips"], positions, rx)
    step = max(float(np.median(np.diff(x))), float(np.median(np.diff(rx))))
    windows: list[tuple[float, float]] = []
    for index in np.argsort(-properties["prominences"]):
        center, width = rx[peaks[index]], widths[index]
        tolerance = float(np.clip(max(step, .35 * width), 1.5, 4.0))
        if not lo < center < hi or np.count_nonzero(np.abs(target_centers - center) <= tolerance) != 1:
            continue
        radius = float(np.clip(2.0 * width, 6., 24.))
        left, right = float(max(lo, center - radius)), float(min(hi, center + radius))
        if any(left < end and right > start for start, end in windows):
            continue
        mask = (x >= left) & (x <= right)
        if np.count_nonzero(mask) < 5:
            continue
        profile = np.interp(x[mask], rx, ry)
        observed = y[mask]
        # Agreement in position alone is too weak for an unidentified input.
        a, b = profile - profile.mean(), observed - observed.mean()
        norm = float(np.linalg.norm(a) * np.linalg.norm(b))
        if norm > 0 and float(np.dot(a, b)) >= .7 * norm:
            windows.append((left, right))
    return tuple(sorted(windows))


def build_measured_phase_subtraction(
    spectrum: ObservedSpectrum,
    reference: ObservedSpectrum,
    *,
    phase_name: str | None = None,
    phase_bands: Sequence[SpectralBand] = (),
    other_bands: Sequence[SpectralBand] = (),
    factor: float | None = None,
    fit_range: tuple[float, float] | None = None,
) -> ProcessingOperation:
    """Build a replayable operation from an already prepared measured profile."""
    windows = None
    if factor is None and fit_range is None:
        if phase_name is None:
            windows = _matching_windows(spectrum, reference)
            if len(windows) < 2:
                raise ValueError("Fewer than two matching bands. Choose a manual fit range or enter the scale.")
        else:
            windows = _diagnostic_windows(spectrum, reference, phase_bands, other_bands)
            if not windows:
                raise ValueError("No isolated reference band was found. Choose a manual fit range or enter the scale.")
    result = build_phase_subtraction_operation(
        spectrum, reference, factor=factor, fit_range=fit_range, fit_windows=windows,
        conservative_scale=phase_name is None and windows is not None,
    )
    parameters = dict(result.parameters)
    parameters["reference_name"] = reference.name or reference.id
    if phase_name is None:
        parameters.pop("phase_name", None)
    else:
        parameters["phase_name"] = phase_name
    return ProcessingOperation(result.kind, parameters)
