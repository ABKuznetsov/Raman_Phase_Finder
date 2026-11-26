from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from vibrational_finder.core.models import CandidateRecord
from vibrational_finder.core.spectral_metadata import excitation_wavelength_nm


@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    profile_id: str
    name: str
    laser_nm: float
    tolerance_nm: float = 2.0
    model: str = ""
    objective: str = ""
    grating: str = ""
    resolution_cm1: float | None = None

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) for value in (self.profile_id, self.name, self.model, self.objective, self.grating)):
            raise ValueError("Invalid instrument profile text fields.")
        if not self.profile_id.strip() or not self.name.strip():
            raise ValueError("Enter a profile name.")
        if not isfinite(self.laser_nm) or not 1 <= self.laser_nm <= 10000:
            raise ValueError("Enter a valid laser wavelength in nm.")
        if not isfinite(self.tolerance_nm) or not 0 <= self.tolerance_nm <= 100:
            raise ValueError("Wavelength tolerance must be between 0 and 100 nm.")
        if self.resolution_cm1 is not None and (
            not isfinite(self.resolution_cm1) or self.resolution_cm1 <= 0
        ):
            raise ValueError("Spectral resolution must be greater than zero.")

    def matches(self, record: CandidateRecord) -> bool:
        wavelength = excitation_wavelength_nm(record)
        return wavelength is not None and abs(wavelength - self.laser_nm) <= self.tolerance_nm + 1.e-9

    def rank_bonus(self, record: CandidateRecord) -> float:
        # A small ordering preference, not part of the spectral Match or Gain score.
        return 3.0 if self.matches(record) else 0.0

    def ranking_score(self, score: float, record: CandidateRecord) -> float:
        return score + self.rank_bonus(record) if score > 0.0 else score
