from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibrational_finder.core.models import CandidateRecord
from vibrational_finder.models import ObservedSpectrum, SpectralBand, VibrationalMatchResult
from vibrational_finder.processing_state import SpectrumProcessingState
from vibrational_finder.raman_gain import GainMatch, GainWindow, RamanPeakPriority

if TYPE_CHECKING:
    from vibrational_finder.phase_subtraction_work import PhaseFitResult


@dataclass(frozen=True, slots=True)
class SavedGain:
    bands: tuple[SpectralBand, ...]
    window: GainWindow
    priority: RamanPeakPriority
    matches: dict[str, GainMatch] = field(default_factory=dict)
    ranked: bool = False
    available: bool = True


@dataclass
class SpectrumProfileState:
    spectrum: ObservedSpectrum | None = None
    results: list[VibrationalMatchResult] = field(default_factory=list)
    residual_results: list[VibrationalMatchResult] = field(default_factory=list)
    browse_records: list[CandidateRecord] = field(default_factory=list)
    selected_results: list[VibrationalMatchResult] = field(default_factory=list)
    visible_selected_candidate_keys: set[str] = field(default_factory=set)
    cumulative_unassigned_bands: list[SpectralBand] = field(default_factory=list)
    processing: SpectrumProcessingState = field(default_factory=SpectrumProcessingState)
    saved_gain: SavedGain | None = None
    phase_fit: PhaseFitResult | None = None
