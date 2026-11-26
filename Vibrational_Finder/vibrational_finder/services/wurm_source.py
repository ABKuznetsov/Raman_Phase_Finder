"""WURM's calculated Raman modes, downloaded explicitly into a local index."""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass
from gzip import GzipFile
from html import unescape
from html.parser import HTMLParser
from http.client import HTTPException, IncompleteRead
from io import BytesIO
from itertools import islice
import json
from math import isfinite
from pathlib import Path
import re
import ssl
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

import certifi

from vibrational_finder.cancellation import checkpoint
from vibrational_finder.core.cache import app_cache_dir
from vibrational_finder.core.data_sources import SourceQuery
from vibrational_finder.core.models import CandidateRecord, SignalKind, SignalTrace
from vibrational_finder.models import ReferenceBandSet, ReferenceSpectrum, SpectralBand
from vibrational_finder.services.reference_cache import ReferenceSpectrumCache
from vibrational_finder.services.research_raman_source import ResearchPackage


WURM_ROOT = "https://www.wurm.info/"
WURM_CATALOG_URL = f"{WURM_ROOT}index.php?id=2"
WURM_NAME = "WURM"
_ARCHIVE_FORMAT = "wurm-raman-1"
_MAX_ARCHIVE_PAYLOAD = 8 * 1024 * 1024
_LICENSE = "CC BY-NC-SA 3.0"
_DOI = "10.2138/am.2011.3532"
_CITATION = "Caracas & Bobocioiu (2011), The WURM project, American Mineralogist 96"
_ENTRY_ID = re.compile(r"w[0-9]{6}")
_HTML_TAG = re.compile(r"</?[^>]+>")
_MAX_XML_BYTES = 12 * 1024 * 1024
_MAX_CATALOG_BYTES = 2 * 1024 * 1024
_DOWNLOAD_WORKERS = 4
_SOCKET_TIMEOUT = 15
_RESPONSE_DEADLINE = 40
_ETAG = re.compile(r'(?:W/)?"[\x21\x23-\x7e]{0,248}"')


@dataclass(frozen=True, slots=True)
class _Response:
    data: bytes | None
    etag: str = ""


class _CatalogLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.identifiers: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        try:
            link = urlsplit(dict(attrs).get("href") or "")
        except ValueError:
            return
        if (
            link.scheme not in ("", "https")
            or link.netloc not in ("", "www.wurm.info", "wurm.info")
            or link.path not in ("index.php", "/index.php")
        ):
            return
        query = parse_qs(link.query)
        identifier = query.get("file", [""])[0]
        if _ENTRY_ID.fullmatch(identifier) and query.get("view") == ["detail"]:
            self.identifiers.add(identifier)


def _catalog_ids(data: bytes) -> list[str]:
    parser = _CatalogLinks()
    parser.feed(data.decode("utf-8-sig"))
    if not 1 <= len(parser.identifiers) <= 1000:
        raise ValueError("WURM did not return a recognized list of mineral records.")
    return sorted(parser.identifiers)


def _fetch(url: str, limit: int, context: ssl.SSLContext, *, if_none_match: str = "") -> _Response:
    headers = {"User-Agent": "Raman-Phase-Finder (WURM reference data)", "Accept-Encoding": "gzip"}
    if if_none_match:
        # WURM's Apache checks the original ETag before appending its gzip suffix.
        if if_none_match.endswith('-gzip"'):
            headers["If-None-Match"] = f'{if_none_match[:-6]}", {if_none_match}'
        else:
            headers["If-None-Match"] = if_none_match
    request = Request(url, headers=headers)
    deadline = monotonic() + _RESPONSE_DEADLINE
    data = bytearray()
    try:
        response = urlopen(request, timeout=_SOCKET_TIMEOUT, context=context)
    except HTTPError as exc:
        if exc.code != 304 or not if_none_match:
            raise
        with exc:
            _check_wurm_url(exc.geturl())
            return _Response(None, if_none_match)
    with response:
        _check_wurm_url(response.geturl())
        etag = response.headers.get("ETag", "")
        if if_none_match and etag == if_none_match:
            return _Response(None, etag)
        encoding = response.headers.get("Content-Encoding", "").lower()
        while len(data) <= limit:
            checkpoint()
            if monotonic() >= deadline:
                raise TimeoutError("The WURM response did not finish in time.")
            chunk = response.read1(min(64 * 1024, limit + 1 - len(data)))
            if not chunk:
                if response.length:
                    raise IncompleteRead(bytes(data), response.length)
                break
            data.extend(chunk)
    if encoding == "gzip" and len(data) <= limit:
        with GzipFile(fileobj=BytesIO(data)) as compressed:
            data = compressed.read(limit + 1)
    elif encoding not in {"", "identity", "gzip"}:
        raise ValueError("The WURM site returned an unsupported compression format.")
    if len(data) > limit:
        raise ValueError("The WURM record exceeds the download size limit.")
    return _Response(bytes(data), etag if _ETAG.fullmatch(etag) else "")


def _check_wurm_url(url: str) -> None:
    location = urlsplit(url)
    if location.scheme != "https" or location.hostname not in {"www.wurm.info", "wurm.info"}:
        raise ValueError("The WURM download was redirected outside its official site.")


def _fetch_with_retry(url: str, limit: int, context: ssl.SSLContext, *, if_none_match: str = "") -> _Response:
    try:
        return _fetch(url, limit, context, if_none_match=if_none_match)
    except HTTPError as exc:
        if exc.code not in {408, 500, 502, 503, 504}:
            raise
        exc.close()
    except URLError as exc:
        if not isinstance(exc.reason, (TimeoutError, ConnectionError)):
            raise
    except (TimeoutError, ConnectionError, IncompleteRead, EOFError):
        pass
    checkpoint()
    return _fetch(url, limit, context, if_none_match=if_none_match)


def _powder_activity(tensor: list[ET.Element]) -> float:
    try:
        rows = [list(map(float, (item.text or "").split())) for item in tensor]
        if len(rows) != 3 or any(len(row) != 3 or not all(isfinite(v) for v in row) for row in rows):
            return 0.0
        a, b, c = (rows[i][i] for i in range(3))
        off_diagonal = [(rows[i][j] + rows[j][i]) / 2 for i, j in ((0, 1), (1, 2), (0, 2))]
        # Powder Raman activity from the isotropic and anisotropic tensor invariants.
        mean = (a + b + c) / 3
        anisotropy = ((a - b)**2 + (b - c)**2 + (c - a)**2 + 6 * sum(v**2 for v in off_diagonal)) / 2
        activity = 45 * mean**2 + 7 * anisotropy
        return activity if isfinite(activity) else 0.0
    except (OverflowError, ValueError):
        return 0.0


def _read_mineral(identifier: str, data: bytes) -> tuple[CandidateRecord, ReferenceBandSet] | None:
    if not _ENTRY_ID.fullmatch(identifier):
        raise ValueError("Invalid WURM identifier.")
    if b"\x00" in data or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data, flags=re.I):
        raise ValueError("Unexpected declarations in a WURM XML record.")
    root = ET.fromstring(data)
    if root.tag != "MINERAL":
        raise ValueError("Expected a WURM MINERAL document.")
    name = (root.findtext("./HEADER/name") or "").strip().title()
    formula = _HTML_TAG.sub("", unescape(root.findtext("./HEADER/formula") or "")).strip()
    if not name or not formula:
        raise ValueError("The WURM mineral has no name or formula.")

    lines: dict[tuple[float, str], float] = {}
    for mode in root.findall("./DYNAMICS/mode"):
        try:
            position = float(mode.findtext("./infoTO/freqTO") or "")
        except ValueError:
            continue
        symmetry = (mode.findtext("char") or "").strip()
        if not isfinite(position) or position <= 0 or symmetry.casefold() == "ac":
            continue
        activity = _powder_activity(mode.findall("./infoTO/raman_tensor/pureTO/l"))
        if activity > 0:
            key = (position, symmetry)
            lines[key] = lines.get(key, 0.0) + activity
    if not lines:
        return None
    strongest = max(lines.values())
    bands = [
        SpectralBand(position=position, intensity=activity / strongest, symmetry=symmetry)
        for (position, symmetry), activity in sorted(lines.items())
    ]
    return _entry(identifier, name, formula, (root.findtext("./STRUCTURE/space-group/spgroup_sym") or "").strip(), bands)


def _entry(
    identifier: str, name: str, formula: str, spacegroup: str, bands: list[SpectralBand],
) -> tuple[CandidateRecord, ReferenceBandSet]:
    url = f"{WURM_ROOT}index.php?file={identifier}&id=2&u_id=2_4&view=detail"
    record = CandidateRecord(
        key=f"{WURM_NAME}:{identifier}", source=WURM_NAME, entry_id=identifier,
        kind=SignalKind.RAMAN, name=name, formula=formula,
        metadata={
            "origin": "calculated", "library_type": "calculated", "dataset": WURM_NAME,
            "reference_provenance": _CITATION, "reference_url": url, "license": _LICENSE,
            "citation": _CITATION, "doi": _DOI,
            "spacegroup": spacegroup,
            "intensity": "Relative powder Raman activity from the published tensor; not measured",
        },
    )
    return record, ReferenceBandSet(bands=bands, origin="calculated", extraction_method="WURM TO powder activity")


def _prepared_entries(path: Path) -> list[tuple[CandidateRecord, ReferenceBandSet]]:
    try:
        with ZipFile(path) as archive:
            members = [item for item in archive.infolist() if item.filename == "wurm.json"]
            if len(members) != 1 or members[0].file_size > _MAX_ARCHIVE_PAYLOAD:
                raise ValueError("Invalid WURM package payload.")
            payload = json.loads(archive.read(members[0]))
    except (BadZipFile, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Cannot read the WURM reference package.") from exc
    if not isinstance(payload, dict) or payload.get("format") != _ARCHIVE_FORMAT:
        raise ValueError("Unsupported WURM reference package format.")
    rows = payload.get("records")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError("Invalid number of records in the WURM package.")
    entries = []
    seen: set[str] = set()
    for row in rows:
        checkpoint()
        if not isinstance(row, dict):
            raise ValueError("Invalid WURM package record.")
        identifier, name, formula, spacegroup = (row.get(key) for key in ("id", "name", "formula", "spacegroup"))
        if (not all(isinstance(value, str) and len(value) <= 512 for value in (identifier, name, formula, spacegroup))
                or not name or not formula or not _ENTRY_ID.fullmatch(identifier) or identifier in seen):
            raise ValueError("Invalid or duplicate WURM package record.")
        seen.add(identifier)
        lines = row.get("bands")
        if not isinstance(lines, list) or not 1 <= len(lines) <= 3000:
            raise ValueError(f"Invalid number of WURM modes for {identifier}.")
        bands = []
        for item in lines:
            if not isinstance(item, list) or len(item) != 3:
                raise ValueError(f"Invalid WURM mode for {identifier}.")
            position, intensity, symmetry = item
            if (type(position) not in (int, float) or not 0 < position < 100000
                    or type(intensity) not in (int, float) or not 0 <= intensity <= 1
                    or not isinstance(symmetry, str) or len(symmetry) > 128):
                raise ValueError(f"Invalid WURM mode for {identifier}.")
            bands.append(SpectralBand(position=position, intensity=intensity, symmetry=symmetry))
        entries.append(_entry(identifier, name, formula, spacegroup, bands))
    return entries


class WurmSource:
    name = WURM_NAME
    band_recipe_version = "wurm-xml-to-powder-v1"

    def __init__(self, cache_root: str | Path | None = None) -> None:
        self.cache_root = Path(cache_root) if cache_root is not None else app_cache_dir() / "wurm"
        self.index_path = self.cache_root / "reference_index.sqlite"
        self.reference_cache: ReferenceSpectrumCache | None = None
        self.refresh_index()

    def refresh_index(self) -> None:
        self.reference_cache = ReferenceSpectrumCache(self.cache_root) if self.index_path.is_file() else None

    def available_libraries(self) -> list[ResearchPackage]:
        return [ResearchPackage("catalog", "Official WURM catalogue", WURM_CATALOG_URL, self.index_path)]

    def status_row(self) -> list[str]:
        count = self.reference_cache.indexed_count(source=self.name) if self.reference_cache else 0
        return [
            self.name, "Indexed" if count else "External",
            f"Calculated TO modes and relative powder activities; {_LICENSE}", str(count), "", str(self.cache_root),
        ]

    def download_library(
        self, key: str | None = None, *, report_progress: Callable[[str], None] | None = None,
    ) -> Path:
        if key not in {None, "catalog"}:
            raise ValueError("Unknown WURM package.")
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
        checkpoint()
        if report_progress is not None:
            report_progress("Reading the catalogue")
        catalogue = _fetch_with_retry(WURM_CATALOG_URL, _MAX_CATALOG_BYTES, context)
        ids = _catalog_ids(catalogue.data or b"")
        previous = self._cached_entries()

        def retrieve(identifier: str) -> tuple[CandidateRecord, ReferenceBandSet] | None:
            existing = previous.get(identifier)
            etag = existing[0].metadata.get("wurm_etag", "") if existing else ""
            try:
                response = _fetch_with_retry(
                    f"{WURM_ROOT}xmls/{identifier}.xml", _MAX_XML_BYTES, context,
                    if_none_match=etag if _ETAG.fullmatch(etag) else "",
                )
                if response.data is None:
                    if existing is None:
                        raise ValueError("WURM returned no data for an uncached record.")
                    return existing
                item = _read_mineral(identifier, response.data)
                if item is not None and response.etag:
                    item[0].metadata["wurm_etag"] = response.etag
                return item
            except (OSError, HTTPException, ET.ParseError, ValueError, EOFError) as exc:
                raise RuntimeError(f"WURM record {identifier}: {exc}") from exc

        entries = []
        completed = 0
        if report_progress is not None:
            report_progress(f"Downloading records: {completed}/{len(ids)}")
        with ThreadPoolExecutor(max_workers=_DOWNLOAD_WORKERS) as pool:
            remaining = iter(ids)

            def submit(identifier: str):
                return pool.submit(copy_context().run, retrieve, identifier)

            pending = {submit(identifier) for identifier in islice(remaining, _DOWNLOAD_WORKERS)}
            while pending:
                checkpoint()
                ready, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in ready:
                    checkpoint()
                    item = future.result()
                    if item is not None:
                        entries.append(item)
                    completed += 1
                    if report_progress is not None:
                        report_progress(f"Downloading records: {completed}/{len(ids)}")
                    if (identifier := next(remaining, None)) is not None:
                        pending.add(submit(identifier))
        if not entries:
            raise ValueError("WURM returned no calculated Raman-active modes.")
        if len(entries) == len(previous) and all(previous.get(item[0].entry_id) is item for item in entries):
            return self.index_path
        if report_progress is not None:
            report_progress(f"Building local index: {len(entries)} Raman references")
        return self._replace_index(entries)

    def _cached_entries(self) -> dict[str, tuple[CandidateRecord, ReferenceBandSet]]:
        if self.reference_cache is None:
            return {}
        records = self.search(SourceQuery(kind=SignalKind.RAMAN))
        bands = self.reference_cache.load_band_sets([record.key for record in records], self.band_recipe_version)
        return {record.entry_id: (record, bands[record.key]) for record in records if record.key in bands}

    def _replace_index(self, entries: list[tuple[CandidateRecord, ReferenceBandSet]]) -> Path:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=self.cache_root, prefix="index-") as staging:
            staged = ReferenceSpectrumCache(staging)
            staged.upsert_records([record for record, _ in entries])
            staged.upsert_band_sets([(record.key, bands) for record, bands in entries], self.band_recipe_version)
            checkpoint()
            staged.index_path.replace(self.index_path)
        self.refresh_index()
        return self.index_path

    def download_library_with_progress(self, key: str | None, report_progress: Callable[[str], None]) -> Path:
        return self.download_library(key, report_progress=report_progress)

    def import_archive(self, key: str, path: str | Path) -> Path:
        if key != "prepared":
            raise ValueError("Select a prepared WURM package.")
        return self._replace_index(_prepared_entries(Path(path)))

    def export_archive(self, path: str | Path) -> Path:
        records = self.search(SourceQuery(kind=SignalKind.RAMAN))
        if not records or self.reference_cache is None:
            raise ValueError("Download WURM before preparing a package.")
        band_sets = self.reference_cache.load_band_sets([record.key for record in records], self.band_recipe_version)
        payload = {"format": _ARCHIVE_FORMAT, "source": WURM_CATALOG_URL, "license": _LICENSE, "records": [
            {"id": record.entry_id, "name": record.name, "formula": record.formula,
             "spacegroup": record.metadata.get("spacegroup", ""),
             "bands": [[band.position, band.intensity, band.symmetry] for band in band_sets[record.key].bands]}
            for record in records
        ]}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("wurm.json", json.dumps(payload, separators=(",", ":"), allow_nan=False))
            archive.writestr("LICENSE.txt", (
                f"WURM Project (Razvan Caracas and Ema Bobocioiu)\n{WURM_CATALOG_URL}\n"
                f"{_CITATION}; https://doi.org/{_DOI}\n\n"
                f"Data license: {_LICENSE}\nhttps://creativecommons.org/licenses/by-nc-sa/3.0/\n"
                "Adapted for Raman Phase Finder: selected Raman-active transverse optical modes, "
                "merged degenerate lines and calculated relative powder activities from the original tensors.\n"
            ))
        return path

    def clear(self) -> None:
        self.index_path.unlink(missing_ok=True)
        self.refresh_index()

    def search(self, query: SourceQuery, *, limit: int = 2000) -> list[CandidateRecord]:
        cache = self.reference_cache
        return cache.search(query, sources=[self.name], limit=limit) if cache else []

    def search_by_bands(
        self, query: SourceQuery, _observed: SignalTrace | None = None, *,
        observed_bands: list[SpectralBand] | None = None, limit: int = 80,
    ) -> list[CandidateRecord]:
        cache = self.reference_cache
        if cache is None:
            return []
        strongest = sorted(observed_bands or (), key=lambda band: band.intensity, reverse=True)[:24]
        return cache.search_by_bands(
            query, [band.position for band in strongest], sources=[self.name],
            recipe_version=self.band_recipe_version, limit=limit,
        )

    def load_spectrum(self, candidate: CandidateRecord) -> ReferenceSpectrum:
        cache = self.reference_cache
        if cache is None or candidate.source != self.name:
            raise KeyError(candidate.key)
        bands = cache.load_band_set(candidate.key, self.band_recipe_version)
        if bands is None:
            raise KeyError(candidate.key)
        return ReferenceSpectrum(x=[], y=[], kind=candidate.kind, name=candidate.name, record=candidate, band_set=bands)
