from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import cast

from vibrational_finder.cancellation import checkpoint
from vibrational_finder.core.models import SignalKind, SignalTrace
from vibrational_finder.io.project_codec import decode_project_state, encode_project_state
from vibrational_finder.project_state import VibrationalProjectState
from vibrational_finder.raman_product import require_raman_project


PORTABLE_PROJECT_SUFFIX = ".rpff"
PORTABLE_MANIFEST_NAME = "project.json"
PROJECT_FORMAT = "raman-phase-finder-project"
PROJECT_SCHEMA_VERSION = 1
MAX_ARCHIVE_MEMBERS = 10_000
MAX_MEMBER_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024


def _safe_member_stem(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe or "asset"


def _trace_payload(trace: SignalTrace) -> dict[str, object]:
    if trace.kind == SignalKind.FTIR:
        raise ValueError("An RPFF project cannot contain infrared traces.")
    return {
        "x": [float(item) for item in trace.x],
        "y": [float(item) for item in trace.y],
        "kind": trace.kind.value,
        "name": trace.name,
        "source_path": trace.source_path,
        "x_unit": trace.x_unit,
        "y_unit": trace.y_unit,
    }


class _ZipAssetWriter:
    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.members_by_content: dict[tuple[str, str], str] = {}
        self.members_by_identity: dict[tuple[str, int], tuple[SignalTrace, str]] = {}

    def store_trace(self, folder: str, key: str, trace: SignalTrace) -> str:
        identity = (folder, id(trace))
        if identity in self.members_by_identity:
            return self.members_by_identity[identity][1]
        payload = json.dumps(
            _trace_payload(trace),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        content_digest = hashlib.sha256(payload).hexdigest()
        content_key = (folder, content_digest)
        member = self.members_by_content.get(content_key)
        if member is None:
            stem = _safe_member_stem(str(key))[:64]
            member = f"{folder}/{stem}-{content_digest}.json"
            self.archive.writestr(member, payload)
            self.members_by_content[content_key] = member
        # Keep the trace alive so its id cannot be reused during this save.
        self.members_by_identity[identity] = (trace, member)
        return member


class _ZipAssetReader:
    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self._references: OrderedDict[tuple[str, type[SignalTrace]], SignalTrace] = OrderedDict()

    def load_trace(self, member: str, cls: type[SignalTrace]) -> SignalTrace:
        checkpoint()
        _validate_member_path(member, ("spectra/observed/", "spectra/processed/", "references/"))
        key = (member, cls)
        if key in self._references:
            self._references.move_to_end(key)
            reference = self._references[key]
            return replace(reference, x=reference.x.copy(), y=reference.y.copy())
        try:
            info = self.archive.getinfo(member)
            data = json.loads(self.archive.read(member).decode("utf-8-sig"))
        except KeyError as exc:
            raise ValueError(f"RPFF project is missing required asset: {member}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"RPFF trace asset must contain an object: {member}")
        try:
            kind = SignalKind(str(data.get("kind", SignalKind.UNKNOWN.value)))
        except ValueError as exc:
            raise ValueError(f"RPFF trace asset has an invalid signal kind: {member}") from exc
        if kind == SignalKind.FTIR:
            raise ValueError("An RPFF project cannot contain infrared traces.")
        try:
            trace = cls(
                x=[float(item) for item in cast(list[object], data.get("x", []))],
                y=[float(item) for item in cast(list[object], data.get("y", []))],
                kind=kind,
                name=str(data.get("name", "")),
                source_path=str(data.get("source_path", "")),
                x_unit=str(data.get("x_unit", "cm-1")),
                y_unit=str(data.get("y_unit", "a.u.")),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"RPFF trace asset contains invalid values: {member}") from exc
        if member.startswith("references/") and info.file_size <= 1024 * 1024:
            self._references[key] = trace
            if len(self._references) > 16:
                self._references.popitem(last=False)
            return replace(trace, x=trace.x.copy(), y=trace.y.copy())
        return trace


def _validate_member_path(member: str, allowed_prefixes: tuple[str, ...]) -> PurePosixPath:
    raw_parts = member.split("/")
    member_path = PurePosixPath(member)
    if (
        not any(member.startswith(prefix) for prefix in allowed_prefixes)
        or "\\" in member
        or ":" in member
        or any(part in {"", ".", ".."} for part in raw_parts)
        or member_path.is_absolute()
        or not member_path.parts
    ):
        raise ValueError(f"Unsafe file path in RPFF project: {member}")
    return member_path


def _target_path(path: str | Path) -> Path:
    target = Path(path)
    if target.suffix.lower() != PORTABLE_PROJECT_SUFFIX:
        target = Path(f"{target}{PORTABLE_PROJECT_SUFFIX}")
    return target


def _validate_archive(archive: zipfile.ZipFile) -> None:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ValueError(
            f"RPFF project contains too many members: {len(members)}; limit is {MAX_ARCHIVE_MEMBERS}."
        )
    names: set[str] = set()
    total_size = 0
    for info in members:
        if info.filename in names:
            raise ValueError(f"RPFF project contains duplicate archive member: {info.filename}")
        names.add(info.filename)
        if info.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
            raise ValueError(
                f"RPFF project member exceeds the uncompressed size limit: {info.filename}"
            )
        total_size += info.file_size
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ValueError("RPFF project exceeds the total uncompressed size limit.")


def save_project(state: VibrationalProjectState, path: str | Path) -> Path:
    require_raman_project(state)
    target = _target_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            writer = _ZipAssetWriter(archive)
            manifest = encode_project_state(state, writer)
            manifest["format"] = PROJECT_FORMAT
            manifest["schema_version"] = PROJECT_SCHEMA_VERSION
            archive.writestr(
                PORTABLE_MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        with zipfile.ZipFile(temporary_path, mode="r") as archive:
            _validate_archive(archive)
            archive.getinfo(PORTABLE_MANIFEST_NAME)
            if archive.testzip() is not None:
                raise ValueError("RPFF project archive verification failed.")
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)
    return target


def load_project(path: str | Path) -> VibrationalProjectState:
    checkpoint()
    source = Path(path)
    if source.suffix.lower() != PORTABLE_PROJECT_SUFFIX:
        raise ValueError("Raman Phase Finder opens only .rpff projects.")
    try:
        with zipfile.ZipFile(source, mode="r") as archive:
            _validate_archive(archive)
            try:
                manifest = json.loads(archive.read(PORTABLE_MANIFEST_NAME).decode("utf-8-sig"))
            except KeyError as exc:
                raise ValueError(f"RPFF project does not contain {PORTABLE_MANIFEST_NAME}.") from exc
            if not isinstance(manifest, dict):
                raise ValueError("RPFF project manifest must contain an object.")
            if manifest.get("format") != PROJECT_FORMAT:
                raise ValueError("This is not a Raman Phase Finder project. Renaming a project does not convert it.")
            version = manifest.get("schema_version")
            if type(version) is not int or version != PROJECT_SCHEMA_VERSION:
                raise ValueError(f"Unsupported RPFF schema version: {version!r}.")
            checkpoint()
            state = decode_project_state(manifest, _ZipAssetReader(archive))
            require_raman_project(state)
            return state
    except zipfile.BadZipFile as exc:
        raise ValueError("This is not a valid Raman Phase Finder (.rpff) project archive.") from exc


def _extract_portable_member(
    archive: zipfile.ZipFile,
    member: str,
    root: Path,
    *,
    allowed_prefix: str,
) -> Path:
    member_path = _validate_member_path(member, (allowed_prefix,))
    resolved_root = root.resolve()
    target = resolved_root.joinpath(*member_path.parts).resolve()
    if target == resolved_root or resolved_root not in target.parents:
        raise ValueError(f"Unsafe file path in RPFF project: {member}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member, mode="r") as source_stream, target.open("wb") as target_stream:
        while block := source_stream.read(1024 * 1024):
            target_stream.write(block)
    return target
