"""Raman-only product constraints for projects and reference cards."""
from __future__ import annotations

from vibrational_finder.core.models import SignalKind
from vibrational_finder.project_state import VibrationalProjectState


def require_raman_project(project: VibrationalProjectState) -> None:
    spectra_and_records = (
        *project.raman_spectra, *project.original_observed.values(),
        *project.reference_spectra, *project.reference_records,
        *(result.candidate for result in project.working_references),
        *(result.candidate for profile in project.profile_states.values()
          for result in (*profile.results, *profile.residual_results, *profile.selected_results)),
    )
    if any(item.kind == SignalKind.FTIR for item in spectra_and_records):
        raise ValueError("Raman Phase Finder projects cannot contain infrared data.")


def is_infrared_card(payload: dict) -> bool:
    metadata = payload.get("metadata") or {}
    return str(payload.get("kind") or metadata.get("method") or "").strip().lower() in {"ftir", "ft-ir", "ir", "infrared"}
