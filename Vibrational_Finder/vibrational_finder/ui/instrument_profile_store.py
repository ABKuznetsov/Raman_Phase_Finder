from __future__ import annotations

from dataclasses import asdict
import json

from PySide6.QtCore import QSettings

from vibrational_finder.core.instrument_profile import InstrumentProfile


_KEY = "instrument/raman_profiles_v1"


class InstrumentProfileStore:
    def __init__(self, settings: QSettings) -> None:
        self.settings = settings
        self._profiles: dict[str, InstrumentProfile] = {}
        self._active_id = ""
        try:
            data = json.loads(settings.value(_KEY, "{}", type=str))
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        rows = data.get("profiles")
        for row in rows if isinstance(rows, list) else ():
            try:
                profile = InstrumentProfile(**row)
            except (AttributeError, TypeError, ValueError):
                continue
            self._profiles[profile.profile_id] = profile
        active = data.get("active")
        if isinstance(active, str) and active in self._profiles:
            self._active_id = active

    @property
    def profiles(self) -> tuple[InstrumentProfile, ...]:
        return tuple(self._profiles.values())

    @property
    def active(self) -> InstrumentProfile | None:
        return self._profiles.get(self._active_id)

    def save(self, profile: InstrumentProfile, *, select: bool = False) -> None:
        if any(existing.profile_id != profile.profile_id and
               existing.name.strip().casefold() == profile.name.strip().casefold() for existing in self.profiles):
            raise ValueError("A profile already has this name.")
        self._profiles[profile.profile_id] = profile
        if select:
            self._active_id = profile.profile_id
        self._write()

    def select(self, profile_id: str | None) -> None:
        if profile_id and profile_id not in self._profiles:
            raise KeyError(profile_id)
        self._active_id = profile_id or ""
        self._write()

    def delete(self, profile_id: str) -> None:
        self._profiles.pop(profile_id, None)
        if self._active_id == profile_id:
            self._active_id = ""
        self._write()

    def _write(self) -> None:
        data = {"active": self._active_id, "profiles": [asdict(profile) for profile in self.profiles]}
        self.settings.setValue(_KEY, json.dumps(data, ensure_ascii=True, separators=(",", ":")))
        self.settings.sync()
