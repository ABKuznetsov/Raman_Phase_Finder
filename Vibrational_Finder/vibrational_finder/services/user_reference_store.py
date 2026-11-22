"""User-owned JSON reference library; the line index is not the source of truth."""
from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from uuid import uuid4

from vibrational_finder.services.reference_card_io import read_reference_card, validate_reference_card, write_json_atomic


_LIBRARY_FORMAT = "vibrational-user-library"
_LOCK = RLock()


class UserReferenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "user_library.json"

    def _load(self) -> dict[str, dict]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        if not isinstance(document, dict) or document.get("format") != _LIBRARY_FORMAT or document.get("version") != 1:
            raise ValueError("Unsupported user library JSON format.")
        entries = document.get("references")
        if not isinstance(entries, list):
            raise ValueError("The user library must contain a references list.")
        records: dict[str, dict] = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
                raise ValueError("A user library entry has no valid ID.")
            if entry["id"] in records:
                raise ValueError(f"Duplicate user library ID: {entry['id']}")
            records[entry["id"]] = validate_reference_card(entry.get("payload"))
        return records

    def _write(self, records: dict[str, dict]) -> None:
        write_json_atomic(self.path, {
            "format": _LIBRARY_FORMAT, "version": 1,
            "references": [{"id": key, "payload": payload} for key, payload in records.items()],
        })

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
