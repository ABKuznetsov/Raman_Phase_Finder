from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from vibrational_finder.models import ObservedSpectrum, SpectralBand, VibrationalMatchResult
from vibrational_finder.raman_gain import GainWindow, gain_band_is_strong
from vibrational_finder.services.phase_deconvolution import phase_reference_bands, retained_raman_evidence


def copy_spectral_bands(bands: Iterable[SpectralBand]) -> list[SpectralBand]:
    return [
        replace(band, assigned_candidate_keys=list(band.assigned_candidate_keys))
        for band in bands
    ]


def raman_gain_remaining_bands(
    spectrum: ObservedSpectrum, accepted: list[VibrationalMatchResult],
) -> list[SpectralBand] | None:
    evidence = _retained_raman_gain_evidence(spectrum, accepted)
    return evidence[0] if evidence is not None else None


def raman_gain_evidence(
    spectrum: ObservedSpectrum, accepted: list[VibrationalMatchResult],
) -> tuple[list[SpectralBand], GainWindow] | None:
    """Automatic Gain can count only stable, unresolved bands of the measured trace."""
    evidence = _retained_raman_gain_evidence(spectrum, accepted)
    if evidence is None:
        return None
    bands, window = evidence
    return [band for band in bands if gain_band_is_strong(band)], window


def _retained_raman_gain_evidence(
    spectrum: ObservedSpectrum, accepted: list[VibrationalMatchResult],
) -> tuple[list[SpectralBand], GainWindow] | None:
    phases = phase_reference_bands(spectrum, accepted)
    if not phases:
        return None
    try:
        return retained_raman_evidence(spectrum, phases)
    except ValueError:
        return None


def _has_position(
    bands: Iterable[SpectralBand],
    position: float,
    tolerance: float,
) -> bool:
    return any(abs(float(band.position) - position) <= tolerance for band in bands)


def cumulative_unassigned_bands(
    results: Iterable[VibrationalMatchResult],
    *,
    position_tolerance_cm1: float = 1.0e-6,
) -> list[SpectralBand]:
    unique_results: list[VibrationalMatchResult] = []
    seen_keys: set[str] = set()
    for result in results:
        key = result.candidate.key
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_results.append(result)

    evidence_results = [result for result in unique_results if result.observed_bands]
    if not evidence_results:
        return []

    cumulative: list[SpectralBand] = []
    for observed in evidence_results[0].observed_bands:
        position = float(observed.position)
        relevant_results = [
            result
            for result in evidence_results
            if _has_position(result.observed_bands, position, position_tolerance_cm1)
        ]
        if relevant_results and all(
            _has_position(result.unassigned_bands, position, position_tolerance_cm1)
            for result in relevant_results
        ):
            cumulative.append(observed)
    return copy_spectral_bands(cumulative)
