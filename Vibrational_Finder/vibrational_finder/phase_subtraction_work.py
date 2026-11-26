"""Detached inputs and outputs for the phase-subtraction worker."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from math import isfinite

import numpy as np

from vibrational_finder.cancellation import checkpoint
from vibrational_finder.core.models import MatchScore
from vibrational_finder.models import ObservedSpectrum, SpectralBand, VibrationalMatchResult
from vibrational_finder.services import phase_deconvolution


@dataclass(frozen=True, slots=True)
class PhaseFitKey:
    spectrum_id: str
    revision: int
    phases: tuple[tuple[str, tuple[tuple[float, str, str, str], ...], int | None, float], ...]


def _supported_shift(result: VibrationalMatchResult) -> float:
    shift = result.score.x_shift
    if not isfinite(shift) or not shift:
        return 0.
    bands = result.reference_bands
    consistent = {
        (index, measured) for index, measured in (result.matched_peak_pairs or ())
        if 0 <= index < len(bands) and isfinite(measured)
        and abs(measured - (bands[index].position + shift)) <= 5.
    }
    if len({index for index, _ in consistent}) < 2 or len({value for _, value in consistent}) < 2:
        return 0.
    return shift


def phase_fit_key(
    spectrum: ObservedSpectrum, revision: int, selected: Sequence[VibrationalMatchResult],
) -> PhaseFitKey:
    phases = []
    for result in selected:
        bands, reference = phase_deconvolution.phase_reference_input(result)
        # Only these fields affect the fit and its labels. Profile-only references
        # are immutable once accepted; replacing one changes its identity.
        labels = tuple((band.position, band.mode, band.symmetry, band.assignment)
                       for band in bands if isfinite(band.position))
        phases.append((result.candidate.key, labels, id(reference) if reference is not None else None,
                       _supported_shift(result)))
    return PhaseFitKey(spectrum.id, revision, tuple(phases))


@dataclass(frozen=True, slots=True)
class PhaseFitRequest:
    key: PhaseFitKey
    spectrum: ObservedSpectrum
    selected: tuple[VibrationalMatchResult, ...]
    saved_result: PhaseFitResult | None = None

    @classmethod
    def snapshot(cls, key: PhaseFitKey, spectrum: ObservedSpectrum,
                 selected: Sequence[VibrationalMatchResult], *,
                 saved_result: PhaseFitResult | None = None) -> PhaseFitRequest:
        inputs = []
        for result in selected:
            bands, reference = phase_deconvolution.phase_reference_input(result)
            if reference is not None and reference.x_unit == spectrum.x_unit:
                reference = replace(reference, x=list(reference.x), y=list(reference.y), band_set=None)
            else:
                reference = None
            inputs.append(VibrationalMatchResult(
                candidate=replace(result.candidate, reference=reference, band_set=None),
                score=MatchScore(), reference_bands=[replace(band) for band in bands],
            ))
        return cls(key, replace(spectrum, x=list(spectrum.x), y=list(spectrum.y)), tuple(inputs), saved_result)


@dataclass(frozen=True, slots=True)
class PhaseFitResult:
    references: dict[str, tuple[SpectralBand, ...]]
    fit: phase_deconvolution.PhaseSubtractionFit | None
    profiles: dict[str, np.ndarray] | None = None


@dataclass(frozen=True, slots=True)
class PhaseFitOutcome:
    request: PhaseFitRequest
    result: PhaseFitResult


class PhaseFitController:
    def __init__(self, apply: Callable[[PhaseFitOutcome], bool]) -> None:
        self.apply_if_current = apply

    def run(self, request: PhaseFitRequest) -> PhaseFitOutcome:
        result = request.saved_result
        if result is None:
            references = phase_deconvolution.phase_reference_bands(request.spectrum, request.selected)
            shifts = {key: shift for key, _, _, shift in request.key.phases if shift}
            fit = (phase_deconvolution.fit_known_phase_subtraction(
                request.spectrum, references, set(references), phase_offsets=shifts,
            ) if references else None)
            result = PhaseFitResult(references, fit)
        return PhaseFitOutcome(request, replace(result, profiles=_fitted_profiles(result.fit)))


def _fitted_profiles(fit: phase_deconvolution.PhaseSubtractionFit | None) -> dict[str, np.ndarray]:
    components: dict[str, np.ndarray] = {}
    if fit is None:
        return components
    for band in fit.bands:
        checkpoint()
        if (not band.removed or band.ambiguous or band.phase_key is None or band.reference_position is None
                or not any(band.curve_y)):
            continue
        if band.phase_key not in components:
            components[band.phase_key] = np.zeros_like(fit.x)
        lo = np.searchsorted(fit.x, band.curve_x[0])
        hi = np.searchsorted(fit.x, band.curve_x[-1], side="right")
        components[band.phase_key][lo:hi] += np.interp(fit.x[lo:hi], band.curve_x, band.curve_y)
    # Gaps keep the fitted lobes distinct from the common background.
    return {key: np.where(values != 0, fit.background + values, np.nan) for key, values in components.items()}
