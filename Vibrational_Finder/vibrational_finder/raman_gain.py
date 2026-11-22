from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite, sqrt
from statistics import quantiles
from typing import Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment

from vibrational_finder.models import SpectralBand


_WEAK_WEIGHT = .25


def gain_band_is_strong(band: SpectralBand) -> bool:
    """Confidence is about the measured peak, not its reference intensity."""
    return band.confidence >= .75


@dataclass(frozen=True, slots=True)
class GainWindow:
    measured: tuple[float, float] | None = None
    masked: tuple[tuple[float, float], ...] = ()

    @classmethod
    def from_coordinates(cls, x: Sequence[float]) -> GainWindow:
        finite = [p for p in x if isfinite(p)]
        return cls((min(finite), max(finite)) if finite else None)

    def includes(self, p: float) -> bool:
        return (
            isfinite(p) and (self.measured is None or self.measured[0] <= p <= self.measured[1])
            and not any(lo <= p <= hi for lo, hi in self.masked)
        )


@dataclass(frozen=True, slots=True)
class GainMatch:
    quality: float
    shift: float
    pairs: tuple[tuple[int, int, float], ...]
    eligible_reference_count: int
    strong_target: int = 0


class RamanPeakPriority(str, Enum):
    OFF = "off"
    POSITION = "position"
    INTENSITY = "intensity"


def raman_priority_weights(
    observed: Sequence[SpectralBand], window: GainWindow, priority: RamanPeakPriority,
) -> tuple[float, ...]:
    """Weights for resolved measured bands only; reference intensities are never used."""
    if priority is RamanPeakPriority.OFF:
        return ()
    intervals = sorted(
        (band.position - radius, band.position + radius, i, band)
        for i, band in enumerate(observed)
        if isfinite(band.position)
        for radius in (max(1.7, min(6., band.width / 2)) if isfinite(band.width) else 1.7,)
    )
    weights = [0.] * len(observed)
    previous_right = float("-inf")
    for j, (lo, hi, i, band) in enumerate(intervals):
        overlaps_neighbor = previous_right >= lo or (j + 1 < len(intervals) and intervals[j + 1][0] <= hi)
        previous_right = max(previous_right, hi)
        if (overlaps_neighbor or not window.includes(band.position) or not gain_band_is_strong(band)
                or any(lo <= end and start <= hi for start, end in window.masked)):
            continue
        if priority is RamanPeakPriority.POSITION:
            weights[i] = 1.
        elif isfinite(band.prominence) and band.prominence > 0:
            weights[i] = band.prominence
    if priority is RamanPeakPriority.INTENSITY:
        maximum = max(weights, default=0.)
        if maximum:
            weights = [sqrt(weight / maximum) for weight in weights]
    return tuple(weights) if sum(weight > 0 for weight in weights) >= 2 else ()


def priority_score(score: float, match: GainMatch, weights: Sequence[float]) -> float:
    """Apply a small ranking bonus only if at least two isolated bands support it."""
    if not weights or len(match.pairs) < 2:
        return score
    strength = sorted((weights[i] / (1. + delta / 2.) for i, _, delta in match.pairs if weights[i] > 0), reverse=True)[:3]
    if len(strength) < 2:
        return score
    return min(100., score * (1. + .12 * sum(strength) / len(strength)))


@dataclass(frozen=True, slots=True)
class GainBandEvidence:
    new_positions: tuple[float, ...]
    masked_reference_positions: tuple[float, ...]
    unexplained_positions: tuple[float, ...]


def describe_gain_bands(
    observed: Sequence[SpectralBand], reference: Sequence[SpectralBand], match: GainMatch,
    *, window: GainWindow,
) -> GainBandEvidence:
    """Keep shared/ambiguous windows distinct from new measured evidence."""
    paired = {i for i, _, _ in match.pairs}
    masked = {
        band.position for band in reference if isfinite(band.position)
        and (window.measured is None or window.measured[0] <= band.position <= window.measured[1])
        and any(lo <= band.position <= hi for lo, hi in window.masked)
    }
    return GainBandEvidence(
        tuple(sorted(observed[i].position for i in paired)),
        tuple(sorted(masked)),
        tuple(sorted(band.position for i, band in enumerate(observed) if i not in paired)),
    )


def compare_gain_bands(
    observed: Sequence[SpectralBand], reference: Sequence[SpectralBand], *,
    window: GainWindow = GainWindow(), max_shift: float = 8.0, shift_step: float = 1.0,
    use_evidence_confidence: bool = False,
) -> GainMatch:
    """Soft two-sided positional coverage with a bounded tolerance per observed band."""
    refs = [(i, band.position) for i, band in enumerate(reference) if window.includes(band.position)]
    query = [(i, band) for i, band in enumerate(observed) if isfinite(band.position)]
    best = GainMatch(0.0, 0.0, (), len(refs))
    if not refs or not query:
        return best

    # Width is only a proxy for positional uncertainty, never an unlimited tolerance.
    widths = np.array([band.width if isfinite(band.width) and band.width > 0 else 0.0 for _, band in query])
    sigma = np.clip(.32 * widths, 1.7, 3.2)
    weak = np.array([not gain_band_is_strong(band) for _, band in query]) if use_evidence_confidence else None
    weighted = weak is not None and bool(weak.any())
    strong_target = min(3, len(query) - int(np.count_nonzero(weak))) if weak is not None else 0
    if strong_target:
        best = GainMatch(0.0, 0.0, (), len(refs), strong_target)
    if weighted:
        weights = np.where(weak, _WEAK_WEIGHT, 1.)
        tolerances = np.where(weak, np.minimum(2.45 * sigma, 3.0), 2.45 * sigma)
        observed_mass = float(weights.sum())
    else:
        observed_mass = len(query)
    positions = np.array([band.position for _, band in query])
    central_range = ()
    if use_evidence_confidence and len(query) >= 4:
        # Use the middle 80% so a stray band at either edge doesn't define the range.
        span_limits = quantiles(positions.tolist(), n=10, method="inclusive")
        if span_limits[-1] > span_limits[0]:
            central_range = (span_limits[0], span_limits[-1])
    differences = positions[:, None] - np.array([p for _, p in refs])[None, :]
    steps = np.arange(max(.1, shift_step), max_shift + 1.e-9, max(.1, shift_step)) if min(len(query), len(refs)) >= 2 else []
    for shift in (0.0, *(s for step in steps for s in (-float(step), float(step)))):
        delta = np.abs(differences - shift)
        scaled = delta / sigma[:, None]
        strength = np.where(scaled <= 2.45, np.exp(-.5 * scaled ** 2), 0.)
        if weighted:
            strength = np.where(delta <= tolerances[:, None], strength * weights[:, None], 0.)
        rows, columns = linear_sum_assignment(-strength)
        positive = strength[rows, columns] > 0
        rows, columns = rows[positive], columns[positive]
        weak_only = False
        strong_pairs = len(rows)
        if weighted:
            weak_pairs = weak[rows]
            if np.count_nonzero(weak_pairs) < 2:
                rows, columns = rows[~weak_pairs], columns[~weak_pairs]
                weak_pairs = weak[rows]
            strong_pairs = len(rows) - int(np.count_nonzero(weak_pairs))
            weak_only = bool(len(rows) and not strong_pairs)
            if shift and weak_only and len(rows) < 3:
                continue  # Don't learn an offset from two marginal peaks alone.
        if shift and len(rows) < 2:
            continue
        shift_penalty = np.exp(-.5 * (shift / max(max_shift, .1)) ** 2)
        quality = float(200 * strength[rows, columns].sum() / (observed_mass + len(refs)) * shift_penalty)
        if weak_only:
            # Even exact weak-only coverage (40/100 before this factor) is tentative.
            quality *= .625
        if strong_target:
            if strong_pairs == 1:
                # Sparse references can have perfect coverage from a single peak;
                # weak supporting bands cannot turn it into a strong identification.
                quality *= .25
            elif strong_pairs == 2 and strong_target == 3:
                quality *= .90  # Prefer a third strong match, without discarding two-band candidates.
        if central_range and len(rows):
            low, high = central_range
            matched_positions = positions[rows]
            covered_span = max(0., min(high, float(matched_positions.max())) - max(low, float(matched_positions.min())))
            # Keep a local or single-band match visible, but prefer support across
            # the residual fingerprint when there is more of it to explain.
            quality *= .5 + .5 * (covered_span / (high - low))
        if quality > best.quality:
            best = GainMatch(
                quality, shift, tuple((query[i][0], refs[j][0], float(delta[i, j])) for i, j in zip(rows, columns)), len(refs),
                strong_target,
            )
    return best
