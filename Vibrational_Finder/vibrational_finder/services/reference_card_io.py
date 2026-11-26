"""Portable Raman cards; JSON for editing, RREF archives for exchange."""
from __future__ import annotations

import json
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from math import isfinite
from pathlib import Path, PureWindowsPath
from tempfile import NamedTemporaryFile
from typing import cast
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from vibrational_finder.core.models import SignalKind
from vibrational_finder.models import spectrum_kind

CARD_FORMAT = "vibrational-reference"
CARD_VERSION = 2
_CARD_MEMBER = "card.json"
_IDENTIFIER = re.compile(r"[\w-]{1,80}", re.ASCII)
_SOURCE_TYPES = {"literature", "database", "measurement", "calculation", "unknown"}
_MAX_MEMBERS = 65
_MAX_CARD_BYTES = 32 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def _finite_number(value: object) -> bool:
    try:
        return not isinstance(value, bool) and isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def reference_profiles(payload: dict) -> list[dict]:
    profiles = payload.get("profiles")
    if profiles is not None:
        return profiles
    old = payload.get("profile")
    return [old] if isinstance(old, dict) and old.get("x") and old.get("y") else []


def is_raman_shift_unit(unit: object) -> bool:
    return str(unit or "").lower().replace(" ", "").replace("−", "-") in {
        "cm-1", "cm^-1", "cm⁻¹", "1/cm", "cm**-1",
    }


def provenance_label(payload: dict) -> str:
    sources = {source["id"]: source for source in payload.get("sources") or [] if isinstance(source, dict) and "id" in source}
    labels = []
    for key in payload.get("source_ids") or []:
        source = sources.get(key, {})
        text = next((str(source[field]) for field in ("citation", "doi", "database", "laboratory", "url", "record_id")
                     if source.get(field)), None)
        labels.append(text or {"literature": "Literature", "database": "Database", "measurement": "Own measurement",
                               "calculation": "Calculation"}.get(source.get("type"), "Unknown source"))
    if labels:
        return "; ".join(dict.fromkeys(labels))
    metadata = payload.get("metadata") or {}
    if payload.get("version", 1) == 1:
        return metadata.get("citation") or metadata.get("doi") or "Unknown source"
    return "Unknown source"


def _validate_trace(profile: dict, context: str, *, minimum: int) -> int:
    x, y = profile.get("x", []), profile.get("y", [])
    if not isinstance(x, list) or not isinstance(y, list) or len(x) != len(y):
        raise ValueError(f"{context} must contain x and y arrays of equal length.")
    if len(x) < minimum or not all(_finite_number(value) for axis in (x, y) for value in axis):
        raise ValueError(f"{context} must contain at least {max(minimum, 2)} finite paired points.")
    return len(x)


def _objects(value: object, label: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must be a list of JSON objects.")
    return value


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value))


def validate_reference_card(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("format", CARD_FORMAT) != CARD_FORMAT:
        raise ValueError("Expected a vibrational-reference card.")
    version = payload.get("version", 1)
    if type(version) is not int or version not in {1, CARD_VERSION}:
        raise ValueError(f"Unsupported reference card version: {version!r}.")
    metadata = payload.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("Reference metadata must be a JSON object.")
    kind = payload.get("kind") or metadata.get("method")
    if not isinstance(kind, str) or spectrum_kind(kind) not in {SignalKind.RAMAN, SignalKind.FTIR}:
        raise ValueError("Set the reference kind to Raman or FTIR.")
    if version == CARD_VERSION and spectrum_kind(kind) is not SignalKind.RAMAN:
        raise ValueError("RREF reference cards support Raman only.")
    if version == CARD_VERSION and not any(str(metadata.get(key) or "").strip() for key in ("name", "formula")):
        raise ValueError("Add the phase name or formula.")
    bands = _objects(payload.get("bands"), "Reference bands")
    has_bands = False
    for number, band in enumerate(bands, 1):
        if band.get("position_cm1") in (None, "") and version == 1:
            continue
        for field in ("position_cm1", "intensity", "fwhm_cm1", "confidence_value", "uncertainty_cm1"):
            value = band.get(field)
            if value in (None, "") and field != "position_cm1":
                continue
            if not _finite_number(value):
                raise ValueError(f"Band {number}: {field} must be a finite number.")
        has_bands = True

    if version == 1:
        profile = payload.get("profile")
        if profile is not None and not isinstance(profile, dict):
            raise ValueError("The reference profile must contain x and y arrays.")
        points = _validate_trace(profile or {}, "The reference profile", minimum=0)
        if not has_bands and points < 2:
            raise ValueError("Add a spectrum or at least one valid band position.")
        return payload

    profiles = _objects(payload.get("profiles"), "Reference profiles")
    if payload.get("profile") is not None:
        raise ValueError("Version 2 uses a profiles list, not a single profile field.")
    if not has_bands and not profiles:
        raise ValueError("Add a spectrum or at least one valid band position.")
    if len(profiles) >= _MAX_MEMBERS:
        raise ValueError("The reference contains too many profiles.")
    sources = _objects(payload.get("sources"), "Reference sources")
    source_ids = set()
    for source in sources:
        key = source.get("id")
        if not _identifier(key) or key in source_ids:
            raise ValueError("Each source must have a unique, valid ID.")
        if not isinstance(source.get("type"), str) or source["type"] not in _SOURCE_TYPES:
            raise ValueError(f"Source {key}: select a supported source type.")
        source_ids.add(key)

    def check_source_links(obj: dict, context: str) -> None:
        selected = obj.get("source_ids")
        if selected is None:
            return
        if not isinstance(selected, list) or any(not isinstance(key, str) for key in selected):
            raise ValueError(f"{context}: source_ids must be a list of source IDs.")
        for key in selected:
            if key not in source_ids:
                raise ValueError(f"{context}: unknown source {key!r}.")

    check_source_links(payload, "Reference")
    profile_ids = set()
    for profile in profiles:
        key = profile.get("id")
        if not _identifier(key) or key in profile_ids:
            raise ValueError("Each profile must have a unique, valid ID.")
        profile_ids.add(key)
        _validate_trace(profile, f"Profile {key}", minimum=2)
        conditions = profile.get("conditions")
        if conditions is not None and not isinstance(conditions, dict):
            raise ValueError(f"Profile {key}: conditions must be an object.")
        conditions = conditions or {}
        for field in ("laser_nm", "temperature_K", "laser_power_mW", "integration_time_s"):
            value = conditions.get(field)
            if value is not None and (not _finite_number(value) or float(value) < 0 or (field == "laser_nm" and float(value) == 0)):
                raise ValueError(f"Profile {key}: invalid {field}.")
        for field in ("x_unit", "y_unit"):
            if profile.get(field) is not None and not isinstance(profile[field], str):
                raise ValueError(f"Profile {key}: {field} must be text.")
        check_source_links(profile, f"Profile {key}")
    for number, band in enumerate(bands, 1):
        check_source_links(band, f"Band {number}")
        profile_id = band.get("profile_id")
        if profile_id is not None and (not isinstance(profile_id, str) or profile_id not in profile_ids):
            raise ValueError(f"Band {number}: unknown profile {profile_id!r}.")
    return payload


@contextmanager
def _atomic_target(path: Path) -> Iterator[Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        yield temporary
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(path: Path, payload: dict) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with _atomic_target(path) as temporary:
        temporary.write_text(content, encoding="utf-8")


def _shareable(value: object) -> object:
    if isinstance(value, dict):
        return {key: _shareable(item) for key, item in value.items()
                if key not in {"source_path", "local_path", "file_path"}
                and not (key == "path" and isinstance(item, str)
                         and (Path(item).is_absolute() or PureWindowsPath(item).is_absolute() or item.startswith("file:")))}
    if isinstance(value, list):
        return [_shareable(item) for item in value]
    return value


def portable_reference_card(payload: dict) -> dict:
    return cast(dict, _shareable(validate_reference_card(payload)))


def _read_rref(path: Path) -> dict:
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_MEMBERS or sum(info.file_size for info in members) > _MAX_ARCHIVE_BYTES:
                raise ValueError("RREF archive exceeds its size or entry limit.")
            names = set()
            for info in members:
                name = info.filename
                if (name in names or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16)
                        or (name != _CARD_MEMBER and not re.fullmatch(r"profiles/[\w-]{1,80}\.tsv", name, re.ASCII))):
                    raise ValueError(f"Unexpected or duplicate RREF archive member: {name}")
                names.add(name)
            if _CARD_MEMBER not in names or archive.getinfo(_CARD_MEMBER).file_size > _MAX_CARD_BYTES:
                raise ValueError("RREF must contain a valid card.json.")
            data = json.loads(archive.read(_CARD_MEMBER).decode("utf-8-sig"))
            if not isinstance(data, dict) or data.get("format") != CARD_FORMAT or data.get("version") != CARD_VERSION:
                raise ValueError("Unsupported RREF card format or version.")
            expected = {_CARD_MEMBER}
            for profile in _objects(data.get("profiles"), "Reference profiles"):
                key = profile.get("id")
                if not _identifier(key):
                    raise ValueError("RREF profile has an invalid ID.")
                member = f"profiles/{key}.tsv"
                if profile.pop("asset", None) != member or member not in names or member in expected:
                    raise ValueError(f"Profile {key}: missing or duplicate attachment.")
                expected.add(member)
                rows = archive.read(member).decode("utf-8").splitlines()
                if not rows or rows[0] != "x\ty":
                    raise ValueError(f"Profile {key}: expected a two-column TSV with x and y.")
                x, y = [], []
                try:
                    for row in rows[1:]:
                        a, b = row.split("\t")
                        x.append(float(a))
                        y.append(float(b))
                except ValueError as exc:
                    raise ValueError(f"Profile {key}: invalid TSV coordinates.") from exc
                profile.update(x=x, y=y)
            if names != expected:
                raise ValueError("RREF contains an unlisted attachment.")
            return validate_reference_card(data)
    except (BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError("Could not read the RREF archive.") from exc


def read_reference_card(path: str | Path) -> dict:
    target = Path(path)
    if target.suffix.lower() == ".rref":
        return _read_rref(target)
    with target.open("rb") as handle:
        data = handle.read(_MAX_CARD_BYTES + 1)
    if len(data) > _MAX_CARD_BYTES:
        raise ValueError("Reference JSON exceeds the file size limit.")
    return validate_reference_card(json.loads(data.decode("utf-8-sig")))


def write_reference_card(path: str | Path, payload: dict) -> Path:
    target = Path(path)
    if not target.suffix:
        target = target.with_suffix(".rref")
    card = portable_reference_card(payload)
    if target.suffix.lower() in {".json", ".vsref"}:
        write_json_atomic(target, card)
        return target
    if target.suffix.lower() != ".rref":
        raise ValueError("Use .rref or .json for a reference card.")
    if card.get("version", 1) == 1:
        profile = card.pop("profile", None)
        metadata = card.get("metadata") or {}
        if profile and len(profile.get("x") or []) >= 2:
            conditions = {key: metadata[key] for key in ("sample_form", "instrument", "orientation", "polarization", "pressure")
                          if metadata.get(key) not in (None, "")}
            for key in ("laser_nm", "temperature_K", "laser_power_mW", "integration_time_s"):
                value = metadata.get(key)
                if _finite_number(value) and float(value) >= 0 and (key != "laser_nm" or float(value) > 0):
                    conditions[key] = float(value)
            card["profiles"] = [{"conditions": conditions, **profile, "id": "measurement-1"}]
        card["bands"] = [band for band in card.get("bands") or [] if band.get("position_cm1") not in (None, "")]
        if not card.get("source_ids") and (metadata.get("doi") or metadata.get("citation")):
            sources = card.setdefault("sources", [])
            key = "literature"
            while any(isinstance(item, dict) and item.get("id") == key for item in sources):
                key += "-1"
            sources.append({"id": key, "type": "literature", **{
                field: metadata[field] for field in ("doi", "citation") if metadata.get(field)
            }})
            card["source_ids"] = [key]
        card["version"] = CARD_VERSION
    card["format"] = CARD_FORMAT
    validate_reference_card(card)
    if spectrum_kind(card.get("kind") or (card.get("metadata") or {}).get("method")) is SignalKind.RAMAN:
        card["kind"] = "raman"
    with _atomic_target(target) as temporary:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=4) as archive:
            size = 0
            for profile in card.get("profiles") or []:
                member = f"profiles/{profile['id']}.tsv"
                trace = "x\ty\n" + "".join(f"{float(x)!r}\t{float(y)!r}\n" for x, y in zip(profile.pop("x"), profile.pop("y")))
                size += len(trace)
                if size > _MAX_ARCHIVE_BYTES:
                    raise ValueError("RREF archive exceeds its size limit.")
                archive.writestr(member, trace)
                profile["asset"] = member
            manifest = json.dumps(card, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            if len(manifest) > _MAX_CARD_BYTES or size + len(manifest) > _MAX_ARCHIVE_BYTES:
                raise ValueError("RREF archive exceeds its size limit.")
            archive.writestr(_CARD_MEMBER, manifest)
    return target
