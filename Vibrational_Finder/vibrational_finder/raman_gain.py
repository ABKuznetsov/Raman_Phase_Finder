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
    aligned = (band.position + match.shift for band in reference)
    masked = {p for p in aligned if isfinite(p)
              and (window.measured is None or window.measured[0] <= p <= window.measured[1])
              and any(lo <= p <= hi for lo, hi in window.masked)}
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
    return GainBandMatcher(
        observed, window=window, max_shift=max_shift, shift_step=shift_step,
        use_evidence_confidence=use_evidence_confidence,
    ).compare_positions([band.position for band in reference])


class GainBandMatcher:
    """Prepare the measured bands once when comparing against a reference library."""

    def __init__(
        self, observed: Sequence[SpectralBand], *, window: GainWindow, max_shift: float, shift_step: float,
        use_evidence_confidence: bool = False,
    ) -> None:
        query = [(i, band) for i, band in enumerate(observed) if isfinite(band.position)]
        self.indices = [i for i, _ in query]
        self.positions = np.asarray([band.position for _, band in query], dtype=float)
        widths = np.asarray([band.width if isfinite(band.width) and band.width > 0 else 0. for _, band in query])
        self.sigma = np.clip(.32 * widths, 1.7, 3.2)
        weak = np.asarray([not gain_band_is_strong(band) for _, band in query], dtype=bool)
        self.strong_target = min(3, len(query) - int(np.count_nonzero(weak))) if use_evidence_confidence else 0
        self.weak = weak if use_evidence_confidence and weak.any() else None
        self.weights = np.where(weak, _WEAK_WEIGHT, 1.) if self.weak is not None else None
        self.observed_mass = float(self.weights.sum()) if self.weights is not None else len(query)
        self.tolerances = 2.45 * self.sigma
        if self.weak is not None:
            self.tolerances[weak] = np.minimum(self.tolerances[weak], 3.)
        self.central_range = ()
        if use_evidence_confidence and len(query) >= 4:
            # A stray band at either edge must not define the range.
            span_limits = quantiles(self.positions.tolist(), n=10, method="inclusive")
            if span_limits[-1] > span_limits[0]:
                self.central_range = (span_limits[0], span_limits[-1])
        steps = np.arange(max(.1, shift_step), max_shift + 1.e-9, max(.1, shift_step)) if len(query) >= 2 else []
        self.shifts = np.asarray([0., *(s for step in steps for s in (-float(step), float(step)))])
        self.penalties = np.exp(-.5 * (self.shifts / max(max_shift, .1)) ** 2)
        self.window = window
        self.shifted_window = use_evidence_confidence or bool(window.masked)

    def _in_window(self, positions: np.ndarray) -> np.ndarray:
        eligible = np.isfinite(positions)
        if self.window.measured is not None:
            low, high = self.window.measured
            eligible &= (positions >= low) & (positions <= high)
        for low, high in self.window.masked:
            eligible &= (positions < low) | (positions > high)
        return eligible

    def compare_positions(self, reference: Sequence[float]) -> GainMatch:
        values = np.asarray(reference, dtype=float)
        nominal = self._in_window(values)
        indices = np.flatnonzero(np.isfinite(values) if self.shifted_window else nominal)
        positions = values[indices]
        best = GainMatch(0., 0., (), int(nominal.sum()), self.strong_target if len(indices) else 0)
        if not len(indices) or not self.indices:
            return best

        shifts = self.shifts if len(indices) >= 2 else self.shifts[:1]
        differences = self.positions[:, None] - positions
        # Exclude only pairs outside tolerance for every possible shift.
        rows, columns = np.nonzero(np.abs(differences) <= (abs(shifts[-1]) + self.tolerances + 1.e-9)[:, None])
        if not len(rows):
            return best
        deltas = np.abs(differences[rows, columns] - shifts[:, None])
        scaled = deltas / self.sigma[rows]
        allowed = scaled <= 2.45
        if self.weak is not None:
            allowed &= deltas <= self.tolerances[rows]
        eligibility = None
        if self.shifted_window and (self.window.measured is not None or self.window.masked):
            eligibility = self._in_window(positions + shifts[:, None])
            allowed &= eligibility[:, columns]

        nonempty_shifts = np.flatnonzero(allowed.any(axis=1))
        for shift_index in nonempty_shifts:
            shift = float(shifts[shift_index])
            edges = np.flatnonzero(allowed[shift_index])
            if shift and len(edges) < 2:
                continue
            r, c = rows[edges], columns[edges]
            strength = np.exp(-.5 * scaled[shift_index, edges] ** 2)
            if self.weights is not None:
                strength *= self.weights[r]
            eligible_count = len(indices) if eligibility is None else int(eligibility[shift_index].sum())
            delta = deltas[shift_index, edges]
            if not _disjoint_pairs(r, c):
                eligible_columns = None if eligibility is None else np.flatnonzero(eligibility[shift_index])
                local_columns = c if eligible_columns is None else np.searchsorted(eligible_columns, c)
                costs = np.zeros((len(self.indices), eligible_count))
                costs[r, local_columns] = strength
                r, c = linear_sum_assignment(-costs)
                strength = costs[r, c]
                positive = strength > 0.
                r, c, strength = r[positive], c[positive], strength[positive]
                if eligible_columns is not None:
                    c = eligible_columns[c]
                delta = np.abs(differences[r, c] - shift)

            strong_pairs = len(r)
            if self.weak is not None:
                weak_count = int(np.count_nonzero(self.weak[r]))
                if weak_count == 1:
                    keep = ~self.weak[r]
                    r, c, delta, strength = r[keep], c[keep], delta[keep], strength[keep]
                    weak_count = 0
                strong_pairs = len(r) - weak_count
            weak_only = bool(len(r) and not strong_pairs)
            if shift and len(r) < (3 if weak_only else 2):
                continue
            quality = float(200 * strength.sum() / (self.observed_mass + eligible_count) * self.penalties[shift_index])
            if weak_only:
                quality *= .625
            if self.strong_target:
                if strong_pairs == 1:
                    quality *= .25
                elif strong_pairs == 2 and self.strong_target == 3:
                    quality *= .90
            if self.central_range and len(r):
                low, high = self.central_range
                matched = self.positions[r]
                covered = max(0., min(high, float(matched.max())) - max(low, float(matched.min())))
                quality *= .5 + .5 * (covered / (high - low))
            if quality > best.quality:
                best = GainMatch(
                    quality, shift,
                    tuple((self.indices[i], int(indices[j]), float(d)) for i, j, d in zip(r, c, delta)),
                    eligible_count, self.strong_target,
                )
        return best


def _disjoint_pairs(rows: np.ndarray, columns: np.ndarray) -> bool:
    # np.nonzero returns row-major order. When no positive edges compete,
    # all of them are optimal without running the assignment solver.
    return (len(rows) < 2 or
            (not (rows[1:] == rows[:-1]).any() and len(set(columns.tolist())) == len(columns)))
