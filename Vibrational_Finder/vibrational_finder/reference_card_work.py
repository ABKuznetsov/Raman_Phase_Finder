"""Work for a manual preview or acceptance, independent of Qt widgets."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from vibrational_finder import matching
from vibrational_finder.band_detection import extract_reference_band_set
from vibrational_finder.cancellation import checkpoint
from vibrational_finder.core.models import CandidateRecord, MatchScore, SignalKind
from vibrational_finder.models import CompoundCandidate, ObservedSpectrum, ReferenceSpectrum, VibrationalMatchResult
from vibrational_finder.preprocessing import preprocess_spectrum


class ReferenceCardSource(Protocol):
    def load_spectrum(self, record: CandidateRecord) -> ReferenceSpectrum: ...


@dataclass(frozen=True, slots=True)
class ReferenceCardRequest:
    record: CandidateRecord
    source: ReferenceCardSource
    spectrum_token: tuple[str | None, int]
    cached: VibrationalMatchResult | None = None
    spectrum: ObservedSpectrum | None = None


@dataclass(frozen=True, slots=True)
class ReferenceCardOutcome:
    request: ReferenceCardRequest
    preview: VibrationalMatchResult
    accepted: VibrationalMatchResult | None


class ReferenceCardController:
    def __init__(self, apply: Callable[[ReferenceCardOutcome], bool]) -> None:
        self.apply_if_current = apply

    def run(self, request: ReferenceCardRequest) -> ReferenceCardOutcome:
        preview = request.cached if request.cached is not None else load_reference_card(request.source, request.record)
        checkpoint()
        spectrum = request.spectrum
        accepted = None
        if spectrum is not None:
            if preview.candidate.kind not in {SignalKind.UNKNOWN, spectrum.kind}:
                raise ValueError("The reference and observed spectrum use different methods")
            reference = preview.candidate.reference
            candidate = replace(preview.candidate, reference=replace(reference) if reference is not None else None)
            accepted = matching.score_candidate(spectrum, candidate)
        return ReferenceCardOutcome(request, preview, accepted)


def load_reference_card(source: ReferenceCardSource, record: CandidateRecord) -> VibrationalMatchResult:
    checkpoint()
    reference = source.load_spectrum(record)
    checkpoint()
    band_set = reference.band_set
    if band_set is None and reference.x and reference.y:
        band_set = extract_reference_band_set(preprocess_spectrum(reference))
    candidate = CompoundCandidate(
        key=record.key, source=record.source, entry_id=record.entry_id, name=record.name,
        formula=record.formula, kind=reference.kind, metadata=dict(record.metadata),
        reference=reference if reference.x and reference.y else None, band_set=band_set,
        card_payload=reference.card_payload,
    )
    bands = list(band_set.bands) if band_set is not None else []
    if candidate.reference is None and not bands:
        raise ValueError("This entry has no spectrum or bands")
    return VibrationalMatchResult(candidate=candidate, score=MatchScore(), reference_bands=bands)
