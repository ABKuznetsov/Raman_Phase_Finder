from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from heapq import heappush, heapreplace

import numpy as np

from finder_core.data_sources import SourceQuery
from finder_core.models import CandidateRecord, MatchScore, SignalKind, SignalTrace
from vibrational_finder.band_detection import BandDetectionOptions, extract_reference_band_set
from vibrational_finder.models import CompoundCandidate, ReferenceBandSet, ReferenceSpectrum, SpectralBand, VibrationalMatchResult
from vibrational_finder.raman_gain import (
    GainMatch, GainWindow, RamanPeakPriority, compare_gain_bands, priority_score, raman_priority_weights,
)
from vibrational_finder.services.staged_search import ReferenceSearchSource


@dataclass(slots=True)
class RamanIndexResult:
    matches: list[VibrationalMatchResult] = field(default_factory=list)
    records: list[CandidateRecord] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    scanned: int = 0
    unreadable: int = 0


@dataclass(slots=True)
class _Hit:
    record: CandidateRecord
    bands: ReferenceBandSet
    match: GainMatch
    reference: ReferenceSpectrum | None = None
    for_gain: bool = False
    ranked_score: float | None = None

    @property
    def score(self) -> float:
        if self.for_gain:
            return self.match.quality
        # A perfect individual line can be diagnostic, but isn't a whole fingerprint.
        support = (0.0, 0.25, 0.78, 1.0)[min(len(self.match.pairs), 3)]
        return self.match.quality * support


def rank_raman_index(
    sources: Iterable[ReferenceSearchSource], query: SourceQuery, observed: SignalTrace,
    observed_bands: list[SpectralBand], *, limit: int, max_shift: float, refine_profiles: bool,
    candidate_gate: Callable[[CandidateRecord], bool] | None = None,
    for_gain: bool = False, gain_window: GainWindow | None = None, shift_step: float = 1.0,
    peak_priority: RamanPeakPriority = RamanPeakPriority.OFF,
) -> RamanIndexResult:
    """Score all eligible local cards before capping Match or Gain results per source."""
    result = RamanIndexResult()
    if not observed_bands:
        return result
    window = gain_window if for_gain and gain_window is not None else GainWindow.from_coordinates(observed.x)
    coarse_step = shift_step if for_gain else 2.0
    seen: set[str] = set()
    detection = BandDetectionOptions(backend="scipy", min_prominence=0.04, min_width_cm1=2.0)
    limit = max(1, limit)
    weights = raman_priority_weights(observed_bands, window, peak_priority)
    pool_limit = 2 * limit if weights else limit

    for source in sources:
        hits: list[tuple[float, int, _Hit]] = []
        scanned = unreadable = 0
        first_error = ""
        try:
            # Only adapters with an authoritative native band index opt into direct
            # cache reads. User/project cards must use their current in-memory catalog.
            recipe = getattr(source, "band_recipe_version", "")
            cache = getattr(source, "reference_cache", None) if recipe else None
            records = cache.search(query, sources=[source.name], limit=-1) if cache is not None else source.search(query)
            records = [r for r in records if r.kind in (SignalKind.RAMAN, SignalKind.UNKNOWN)
                       and query.allows_formula(r.formula) and (candidate_gate is None or candidate_gate(r))]
            for start in range(0, len(records), 400):
                batch = records[start:start + 400]
                indexed = cache.load_band_sets([row.key for row in batch], recipe) if cache is not None else {}
                for record in batch:
                    if record.key in seen:
                        continue
                    scanned += 1
                    bands = indexed.get(record.key)
                    reference = None
                    if bands is None:
                        try:
                            reference = source.load_spectrum(record)
                            bands = reference.band_set
                            if bands is None and reference.x and reference.y:
                                bands = extract_reference_band_set(reference, detection,
                                                                  origin=record.metadata.get("origin") or "experimental")
                        except Exception as exc:
                            unreadable += 1
                            first_error = first_error or f"{type(exc).__name__}: {exc}"
                            continue
                    seen.add(record.key)
                    if bands is None or not bands.bands:
                        continue
                    match = compare_gain_bands(observed_bands, bands.bands, window=window,
                                               max_shift=max_shift, shift_step=coarse_step, use_evidence_confidence=for_gain)
                    if match.pairs:
                        hit = _Hit(record, bands, match, reference, for_gain=for_gain)
                        item = (hit.score, -scanned, hit)
                        if len(hits) < pool_limit:
                            heappush(hits, item)
                        elif item[:2] > hits[0][:2]:
                            heapreplace(hits, item)
        except Exception as exc:
            result.failures[source.name] = f"{type(exc).__name__}: {exc}"
        result.scanned += scanned
        result.unreadable += unreadable
        if unreadable and unreadable == scanned:
            result.failures.setdefault(source.name, first_error)

        # An optional extra pass reorders a wider, already scored shortlist using
        # cached matched pairs. It neither revisits the database nor loads extra profiles.
        ordered = sorted(hits, reverse=True)
        if weights:
            for _, _, hit in ordered:
                hit.ranked_score = priority_score(hit.score, hit.match, weights)
            ordered.sort(key=lambda item: item[2].ranked_score, reverse=True)
        for _, _, hit in ordered[:limit]:
            if hit.reference is None:
                try:
                    hit.reference = source.load_spectrum(hit.record)
                except Exception:
                    pass  # An indexed band-only match still exists without its profile.
            if not for_gain and refine_profiles and _profile_supports_alignment(hit):
                refined = compare_gain_bands(observed_bands, hit.bands.bands, window=window,
                                             max_shift=max_shift, shift_step=0.5)
                if replace(hit, match=refined).score > hit.score:
                    hit.match = refined
                    if weights:
                        hit.ranked_score = priority_score(hit.score, refined, weights)
            result.records.append(hit.record)
            result.matches.append(_make_result(hit, observed_bands))

    result.matches.sort(key=lambda row: row.score.combined, reverse=True)
    record_by_key = {row.key: row for row in result.records}
    result.records = [record_by_key[row.candidate.key] for row in result.matches]
    return result


def _profile_supports_alignment(hit: _Hit) -> bool:
    if hit.reference is None or len(hit.match.pairs) < 2:
        return False
    x = np.asarray(hit.reference.x, dtype=float)
    y = np.asarray(hit.reference.y, dtype=float)
    if x.size < 5 or y.size != x.size or not np.isfinite(x).all() or not np.isfinite(y).all():
        return False
    # Check that the trace supports the indexed lines before refining shift. Do
    # not compare relative heights, widths or shape between different samples.
    supported = 0
    for _, j, _ in hit.match.pairs:
        position = hit.bands.bands[j].position
        indices = np.flatnonzero(np.abs(x - position) <= 6.0)
        if indices.size < 3:
            continue
        local = y[indices]
        peak = int(np.argmax(local))
        if 0 < peak < len(local) - 1 and local[peak] > max(local[0], local[-1]):
            supported += 1
            if supported >= 2:
                return True
    return False


def _make_result(hit: _Hit, observed: list[SpectralBand]) -> VibrationalMatchResult:
    record, bands, match = hit.record, hit.bands.bands, hit.match
    reference = hit.reference
    if reference is not None and (not reference.x or not reference.y or len(reference.x) != len(reference.y)):
        reference = None
    paired = {i for i, _, _ in match.pairs}
    reference_candidate = CompoundCandidate(
        record.key, record.source, record.entry_id, record.name, record.formula, record.kind,
        dict(record.metadata), reference, hit.bands,
    )
    aligned = None if reference is None else replace(
        reference, x=[value + match.shift for value in reference.x], band_set=hit.bands,
    )
    return VibrationalMatchResult(
        candidate=reference_candidate,
        score=MatchScore(combined=hit.ranked_score if hit.ranked_score is not None else hit.score, position=match.quality,
                         coverage=100.0 * len(paired) / len(observed), matched_features=len(paired),
                         total_features=len(observed), x_shift=match.shift),
        observed_bands=observed, reference_bands=bands,
        unassigned_bands=[band for i, band in enumerate(observed) if i not in paired],
        aligned_reference=aligned,
        matched_peak_pairs=[(j, observed[i].position) for i, j, _ in match.pairs],
    )
