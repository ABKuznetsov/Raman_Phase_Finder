"""Snapshots and bounded, session-only preparation of automatic Raman Gain."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from math import isfinite
from threading import Lock

from vibrational_finder.cancellation import checkpoint
from vibrational_finder.core.models import MatchScore, SignalKind
from vibrational_finder.models import ObservedSpectrum, SpectralBand, VibrationalMatchResult
from vibrational_finder.profile_state import SpectrumProfileState
from vibrational_finder.raman_gain import GainMatch, GainWindow, compare_gain_bands
from vibrational_finder.unassigned_bands import copy_spectral_bands, raman_gain_evidence


@dataclass(frozen=True, slots=True)
class GainCacheKey:
    spectrum_id: str
    processing_revision: int
    phases: tuple[tuple[str, float, tuple[float, ...], int | None], ...]


@dataclass(frozen=True, slots=True)
class GainInput:
    key: GainCacheKey
    spectrum: ObservedSpectrum
    accepted: tuple[VibrationalMatchResult, ...]
    prepared: GainEvidence | None = None


def snapshot_gain(profile: SpectrumProfileState | None) -> GainInput | None:
    if profile is None or not profile.selected_results:
        return None
    spectrum = profile.spectrum
    if spectrum is None or spectrum.kind != SignalKind.RAMAN:
        return None
    if (saved := profile.saved_gain) is not None:
        key = GainCacheKey(spectrum.id, profile.processing.revision, ())
        evidence = GainEvidence(tuple(copy_spectral_bands(saved.bands)), saved.window, saved.available)
        return GainInput(key, replace(spectrum, x=spectrum.x.copy(), y=spectrum.y.copy()), (), evidence)
    phases = []
    definitions = []
    for result in profile.selected_results:
        candidate = result.candidate
        reference = candidate.reference
        bands = result.reference_bands or (candidate.band_set.bands if candidate.band_set else [])
        if not bands and reference is not None and reference.band_set is not None:
            bands = reference.band_set.bands
        shift = float(result.score.x_shift)
        definitions.append((candidate.key, shift, tuple(band.position for band in bands if isfinite(band.position)),
                            id(reference) if reference is not None and not bands else None))
        fallback = (replace(reference, x=list(reference.x), y=list(reference.y), band_set=None)
                    if reference is not None and not bands else None)
        phases.append(VibrationalMatchResult(
            candidate=replace(candidate, reference=fallback, band_set=None),
            score=MatchScore(x_shift=shift), reference_bands=copy_spectral_bands(bands),
        ))
    key = GainCacheKey(spectrum.id, profile.processing.revision, tuple(definitions))
    return GainInput(key, replace(spectrum, x=list(spectrum.x), y=list(spectrum.y)), tuple(phases))


@dataclass(frozen=True, slots=True)
class GainEvidence:
    bands: tuple[SpectralBand, ...]
    window: GainWindow
    available: bool = True


class GainEvidenceCache:
    def __init__(self, max_entries: int = 12) -> None:
        self._entries: OrderedDict[GainCacheKey, GainEvidence] = OrderedDict()
        self._max_entries = max_entries
        self._lock = Lock()

    def lookup(self, work: GainInput) -> GainEvidence | None:
        with self._lock:
            value = self._entries.get(work.key)
            if value is not None:
                self._entries.move_to_end(work.key)
            return value

    def prepare(self, work: GainInput) -> GainEvidence:
        checkpoint()
        if work.prepared is not None:
            return work.prepared
        cached = self.lookup(work)
        if cached is not None:
            return cached
        result = raman_gain_evidence(work.spectrum, list(work.accepted))
        value = GainEvidence(tuple(result[0]), result[1]) if result is not None else GainEvidence((), GainWindow(), False)
        with self._lock:
            self._entries[work.key] = value
            self._entries.move_to_end(work.key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        checkpoint()
        return value


@dataclass(frozen=True, slots=True)
class GainTableRequest:
    input: GainInput
    references: tuple[tuple[str, tuple[float, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class GainTableOutcome:
    request: GainTableRequest
    evidence: GainEvidence
    matches: dict[str, GainMatch]


class GainTableController:
    def __init__(self, cache: GainEvidenceCache, apply: Callable[[GainTableOutcome], bool]) -> None:
        self._cache = cache
        self.apply_if_current = apply

    def run(self, request: GainTableRequest) -> GainTableOutcome:
        evidence = self._cache.prepare(request.input)
        matches: dict[str, GainMatch] = {}
        if evidence.bands:
            for key, positions in request.references:
                checkpoint()
                matches[key] = compare_gain_bands(
                    evidence.bands, [SpectralBand(position, 0.0) for position in positions],
                    window=evidence.window, use_evidence_confidence=True,
                )
        return GainTableOutcome(request, evidence, matches)
