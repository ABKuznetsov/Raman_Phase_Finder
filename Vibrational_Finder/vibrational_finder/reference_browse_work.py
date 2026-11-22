"""Query reference metadata without reading reference spectra or touching widgets."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from vibrational_finder.cancellation import JobCancelled, checkpoint
from vibrational_finder.core.data_sources import SourceQuery
from vibrational_finder.core.models import CandidateRecord
from vibrational_finder.reference_card_work import ReferenceCardSource
from vibrational_finder.services.spectroscopy_source_catalog import CatalogSourceHandle


@dataclass(frozen=True, slots=True)
class ReferenceBrowseRequest:
    sources: tuple[CatalogSourceHandle, ...]
    query: SourceQuery


@dataclass(frozen=True, slots=True)
class ReferenceBrowseOutcome:
    request: ReferenceBrowseRequest
    records: list[CandidateRecord]
    source_by_key: dict[str, ReferenceCardSource]
    unavailable: tuple[str, ...]


class ReferenceBrowseController:
    def __init__(self, apply: Callable[[ReferenceBrowseOutcome], bool]) -> None:
        self.apply_if_current = apply

    def run(self, request: ReferenceBrowseRequest) -> ReferenceBrowseOutcome:
        records = []
        source_by_key = {}
        failures = []
        for handle in request.sources:
            checkpoint()
            try:
                source = handle.open()
                for record in source.search(request.query):
                    checkpoint()
                    if record.key not in source_by_key and request.query.allows_formula(record.formula):
                        source_by_key[record.key] = source
                        records.append(record)
            except JobCancelled:
                raise
            except Exception:
                failures.append(handle.name)
        return ReferenceBrowseOutcome(request, records, source_by_key, tuple(sorted(failures)))
