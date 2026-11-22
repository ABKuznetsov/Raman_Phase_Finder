"""Detached inputs and outputs for the phase-subtraction worker."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from math import isfinite

from vibrational_finder.core.models import MatchScore
from vibrational_finder.models import ObservedSpectrum, SpectralBand, VibrationalMatchResult
from vibrational_finder.services import phase_deconvolution


@dataclass(frozen=True, slots=True)
class PhaseFitKey:
    spectrum_id: str
    revision: int
    phases: tuple[tuple[str, tuple[tuple[float, str, str, str], ...], int | None], ...]


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
        phases.append((result.candidate.key, labels, id(reference) if reference is not None else None))
    return PhaseFitKey(spectrum.id, revision, tuple(phases))


@dataclass(frozen=True, slots=True)
class PhaseFitRequest:
    key: PhaseFitKey
    spectrum: ObservedSpectrum
    selected: tuple[VibrationalMatchResult, ...]

    @classmethod
    def snapshot(cls, key: PhaseFitKey, spectrum: ObservedSpectrum,
                 selected: Sequence[VibrationalMatchResult]) -> PhaseFitRequest:
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
        return cls(key, replace(spectrum, x=list(spectrum.x), y=list(spectrum.y)), tuple(inputs))


@dataclass(frozen=True, slots=True)
class PhaseFitResult:
    references: dict[str, tuple[SpectralBand, ...]]
    fit: phase_deconvolution.PhaseSubtractionFit | None


@dataclass(frozen=True, slots=True)
class PhaseFitOutcome:
    request: PhaseFitRequest
    result: PhaseFitResult


class PhaseFitController:
    def __init__(self, apply: Callable[[PhaseFitOutcome], bool]) -> None:
        self.apply_if_current = apply

    def run(self, request: PhaseFitRequest) -> PhaseFitOutcome:
        references = phase_deconvolution.phase_reference_bands(request.spectrum, request.selected)
        fit = (phase_deconvolution.fit_known_phase_subtraction(request.spectrum, references, set(references))
               if references else None)
        return PhaseFitOutcome(request, PhaseFitResult(references, fit))
