from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from vibrational_finder.core.models import SignalTrace
from vibrational_finder.models import VibrationalMatchResult
from vibrational_finder.project_state import VibrationalProjectState


def _copy_axes(memo: dict[int, object], *axes: object) -> None:
    for axis in axes:
        if isinstance(axis, list) and id(axis) not in memo:
            memo[id(axis)] = axis.copy()


def copy_trace_coordinates(memo: dict[int, object], trace: SignalTrace | None) -> None:
    if trace is not None:
        _copy_axes(memo, trace.x, trace.y)


def copy_match_coordinates(memo: dict[int, object], result: VibrationalMatchResult) -> None:
    copy_trace_coordinates(memo, result.candidate.reference)
    copy_trace_coordinates(memo, result.aligned_reference)
    if result.candidate.card_payload is not None:
        from vibrational_finder.services.reference_card_io import reference_profiles

        for profile in reference_profiles(result.candidate.card_payload):
            _copy_axes(memo, profile.get("x"), profile.get("y"))


def snapshot_project(state: VibrationalProjectState) -> VibrationalProjectState:
    """Freeze the live document for writing without deep-copying every float."""
    memo: dict[int, object] = {}

    for trace in (*state.raman_spectra, *state.original_observed.values(), *state.reference_spectra):
        copy_trace_coordinates(memo, trace)
    for result in state.working_references:
        copy_match_coordinates(memo, result)
    for profile in state.profile_states.values():
        copy_trace_coordinates(memo, profile.spectrum)
        for result in (*profile.results, *profile.residual_results, *profile.selected_results):
            copy_match_coordinates(memo, result)
    return deepcopy(state, memo)


@dataclass(slots=True)
class ProjectDocument:
    """Runtime lifecycle state around the portable spectroscopy project model."""

    state: VibrationalProjectState
    path: Path | None = None
    revision: int = 0
    saved_revision: int = 0

    def __post_init__(self) -> None:
        self.state.normalize()
        if self.path is not None:
            self.path = Path(self.path)

    @property
    def dirty(self) -> bool:
        return self.revision != self.saved_revision

    def replace_state(self, state: VibrationalProjectState, *, path: Path | None) -> None:
        state.normalize()
        self.state = state
        self.path = Path(path) if path is not None else None
        self.revision = 0
        self.saved_revision = 0

    def mark_changed(self) -> None:
        self.revision += 1

    def mark_saved(self, path: Path, *, revision: int | None = None) -> None:
        self.path = Path(path)
        self.saved_revision = self.revision if revision is None else revision
