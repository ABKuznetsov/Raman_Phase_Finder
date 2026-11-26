"""User-owned JSON reference library; the line index is not the source of truth."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from threading import RLock
from uuid import uuid4

from vibrational_finder.raman_product import is_infrared_card
from vibrational_finder.services.reference_card_io import (
    portable_reference_card, read_reference_card, validate_reference_card, write_json_atomic,
)


_LIBRARY_FORMAT = "vibrational-user-library"
_LOCK = RLock()


@dataclass(frozen=True)
class LibraryImportResult:
    added: int
    duplicates: int
    skipped_infrared: int
    conflicts: int
    backup_path: Path | None


def _read_library(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8-sig") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or document.get("format") != _LIBRARY_FORMAT:
        raise ValueError("Select a user library JSON file, not an individual reference card.")
    if type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError(f"Unsupported library version: {document.get('version')!r}.")
    entries = document.get("references")
    if not isinstance(entries, list):
        raise ValueError("The user library must contain a references list.")
    records: dict[str, dict] = {}
    for number, entry in enumerate(entries, 1):
        card_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(card_id, str) or not card_id.strip():
            raise ValueError(f"Entry {number}: missing or invalid ID.")
        if card_id in records:
            raise ValueError(f"Entry {number}: duplicate user library ID.")
        try:
            records[card_id] = validate_reference_card(entry.get("payload"))
        except ValueError as exc:
            raise ValueError(f"Entry {number}: {exc}") from exc
    return records


def _write_library(path: Path, records: dict[str, dict]) -> None:
    write_json_atomic(path, {
        "format": _LIBRARY_FORMAT, "version": 1,
        "references": [{"id": key, "payload": payload} for key, payload in records.items()],
    })


def _portable_id(card_id: str) -> bool:
    return not ("/" in card_id or "\\" in card_id or PureWindowsPath(card_id).drive or card_id.lower().startswith("file:"))


def _fingerprint(card: dict) -> bytes:
    data = json.dumps(card, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(data.encode("ascii")).digest()


class UserReferenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "user_library.json"

    def _load(self) -> dict[str, dict]:
        try:
            return _read_library(self.path)
        except FileNotFoundError:
            return {}

    def _write(self, records: dict[str, dict]) -> None:
        _write_library(self.path, records)

    def _collect(self) -> tuple[dict[str, dict], bool]:
        records = self._load()
        migrated = False
        for path in sorted(self.root.glob("*.vsref")):
            key = str(path.resolve())
            if key in records:
                continue
            try:
                records[key] = read_reference_card(path)
            except (OSError, ValueError):
                continue
            migrated = True
        return records, migrated

    def cards(self) -> dict[str, dict]:
        with _LOCK:
            records, migrated = self._collect()
            if migrated:
                self._write(records)
            return records

    def save(self, payload: dict, *, card_id: str | None = None) -> str:
        validate_reference_card(payload)
        with _LOCK:
            records, _ = self._collect()
            if card_id is not None and card_id not in records:
                raise ValueError("This reference no longer exists in the user library.")
            identifier = card_id or uuid4().hex
            records[identifier] = payload
            self._write(records)
            return identifier

    def export_library(self, path: Path) -> int:
        if path.resolve() == self.path.resolve():
            raise ValueError("Choose a different file from the active user library.")
        with _LOCK:
            records, _ = self._collect()
            exported = {
                key if _portable_id(key) else uuid4().hex: portable_reference_card(card)
                for key, card in records.items() if not is_infrared_card(card)
            }
            _write_library(path, exported)
        return len(exported)

    def import_library(self, path: Path) -> LibraryImportResult:
        if path.resolve() == self.path.resolve():
            raise ValueError("This is the active user library. Select a different file to import.")
        incoming = _read_library(path)
        with _LOCK:
            records, _ = self._collect()
            merged = records.copy()
            seen = {_fingerprint(portable_reference_card(card)) for card in records.values() if not is_infrared_card(card)}
            added = duplicates = skipped_infrared = conflicts = 0
            for key, card in incoming.items():
                if is_infrared_card(card):
                    skipped_infrared += 1
                    continue
                portable = portable_reference_card(card)
                fingerprint = _fingerprint(portable)
                if fingerprint in seen:
                    duplicates += 1
                    continue
                conflicts += key in merged
                identifier = key if key not in merged and _portable_id(key) else uuid4().hex
                merged[identifier] = portable
                seen.add(fingerprint)
                added += 1
            backup = None
            if added:
                if records or self.path.exists():
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup = self.root / f"user_library.before_import_{stamp}_{uuid4().hex[:8]}.json"
                    _write_library(backup, records)
                self._write(merged)
        return LibraryImportResult(added, duplicates, skipped_infrared, conflicts, backup)
