"""Export the raw Raman reference, without any search/plot alignment."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path, PureWindowsPath
import re

from vibrational_finder.cancellation import checkpoint
from vibrational_finder.core.models import SignalKind
from vibrational_finder.core.spectral_metadata import excitation_wavelength_nm
from vibrational_finder.models import VibrationalMatchResult
from vibrational_finder.reference_card_work import ReferenceCardRequest, load_reference_card
from vibrational_finder.services.reference_card_io import CARD_FORMAT, CARD_VERSION, write_reference_card


@dataclass(frozen=True, slots=True)
class ReferenceExportRequest:
    items: tuple[VibrationalMatchResult | ReferenceCardRequest, ...]
    destination: Path
    suffix: str
    batch: bool = False


@dataclass(frozen=True, slots=True)
class ReferenceExportOutcome:
    request: ReferenceExportRequest
    saved: tuple[Path, ...]
    errors: tuple[str, ...]


def _record_id(value: str) -> str:
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or value.startswith("file:"):
        return ""
    return value


def _payload(result: VibrationalMatchResult) -> dict:
    candidate = result.candidate
    if candidate.kind is SignalKind.FTIR:
        raise ValueError("Only Raman reference cards can be exported.")
    if candidate.card_payload is not None:
        return candidate.card_payload
    metadata = {
        field: candidate.metadata[field]
        for field in ("mineral", "origin", "citation", "doi", "url", "description", "sample", "sample_form",
                      "instrument", "orientation", "polarization", "acquisition_mode")
        if candidate.metadata.get(field)
    }
    metadata.update(name=candidate.name or candidate.entry_id, formula=candidate.formula)
    laser = excitation_wavelength_nm(candidate)
    if laser is not None:
        metadata["laser_nm"] = laser
    bands = (candidate.band_set.bands if candidate.band_set and candidate.band_set.bands else result.reference_bands)
    source = {"id": "reference-source", "type": "database"} if candidate.source else {
        "id": "reference-source", "type": "unknown",
    }
    if candidate.source:
        source["database"] = candidate.source
    if entry_id := _record_id(candidate.entry_id):
        source["record_id"] = entry_id
    source.update({field: metadata[field] for field in ("citation", "doi", "url") if field in metadata})
    sources = [source]
    source_ids = ["reference-source"]
    if metadata.get("doi") or metadata.get("citation"):
        sources.append({"id": "literature", "type": "literature", **{
            field: metadata[field] for field in ("doi", "citation") if field in metadata
        }})
        source_ids.append("literature")
    profiles = []
    reference = candidate.reference
    if reference is not None and len(reference.x) == len(reference.y):
        points = [(float(x), float(y)) for x, y in zip(reference.x, reference.y)
                  if isfinite(float(x)) and isfinite(float(y))]
        if len(points) >= 2:
            x, y = zip(*points)
            profiles.append({
                "id": "source-profile", "x": list(x), "y": list(y),
                "x_unit": reference.x_unit or None, "y_unit": reference.y_unit or None,
                "conditions": {field: metadata[field] for field in (
                    "laser_nm", "sample_form", "instrument", "orientation", "polarization", "acquisition_mode",
                ) if field in metadata},
                "source_ids": source_ids,
            })
    return {
        "format": CARD_FORMAT, "version": CARD_VERSION, "kind": "raman",
        "source": candidate.source, "entry_id": _record_id(candidate.entry_id), "metadata": metadata,
        "sources": sources, "source_ids": source_ids, "profiles": profiles,
        "bands": [
            {"position_cm1": float(band.position),
             "intensity": float(band.intensity) if isfinite(float(band.intensity)) and band.intensity > 0 else None,
             "fwhm_cm1": float(band.width) if isfinite(float(band.width)) and band.width > 0 else None,
             "mode": band.mode, "assignment": band.assignment, "symmetry": band.symmetry,
             "polarization": band.polarization, "orientation": band.orientation, "comment": band.source_comment,
             "source_ids": source_ids}
            for band in bands if isfinite(float(band.position))
        ],
    }


def reference_file_stem(name: str, source: str, entry_id: str) -> str:
    stem = re.sub(r'[^\w .-]+', '_', ' - '.join(part for part in (name, source, _record_id(entry_id)) if part))
    return stem.strip(" ._-")[:64].rstrip(" .") or "Raman reference"


def _reserve_target(folder: Path, stem: str, suffix: str) -> Path:
    number = 1
    while True:
        tail = "" if number == 1 else f" ({number})"
        target = folder / f"{stem}{tail}{suffix}"
        try:
            target.touch(exist_ok=False)
        except FileExistsError:
            number += 1
        else:
            return target


class ReferenceExportController:
    def __init__(self, apply: Callable[[ReferenceExportOutcome], bool]) -> None:
        self.apply_if_current = apply

    def run(self, request: ReferenceExportRequest) -> ReferenceExportOutcome:
        saved, errors = [], []
        for item in request.items:
            checkpoint()
            candidate = item.record if isinstance(item, ReferenceCardRequest) else item.candidate
            name = candidate.name or candidate.entry_id
            reserved: Path | None = None
            try:
                if isinstance(item, ReferenceCardRequest):
                    item = item.cached if item.cached is not None else load_reference_card(item.source, item.record)
                payload = _payload(item)
                target = request.destination
                if request.batch:
                    stem = reference_file_stem(name, candidate.source, candidate.entry_id)
                    reserved = target = _reserve_target(target, stem, request.suffix)
                saved.append(write_reference_card(target, payload))
            except (OSError, ValueError, RuntimeError) as exc:
                if reserved is not None:
                    reserved.unlink(missing_ok=True)
                errors.append(f"{name}: {exc}")
        return ReferenceExportOutcome(request, tuple(saved), tuple(errors))
