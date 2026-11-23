from __future__ import annotations

from dataclasses import dataclass
import filecmp
from importlib.util import find_spec
from pathlib import Path
import shutil
import ssl
import urllib.request
import warnings

import numpy as np

from vibrational_finder.core.cache import app_cache_dir
from vibrational_finder.core.data_sources import SourceQuery
from vibrational_finder.core.models import CandidateRecord, SignalKind
from vibrational_finder.band_detection import BandDetectionOptions, detect_bands, extract_reference_band_set
from vibrational_finder.cancellation import JobCancelled, checkpoint
from vibrational_finder.models import CompoundCandidate, ReferenceBandSet, ReferenceSpectrum, SpectralBand
from vibrational_finder.services.openspecy_profile_cache import archive_stamp, profile_cache_path, read_profile, write_profiles
from vibrational_finder.services.reference_cache import ReferenceSpectrumCache


OPENSPECY_LIBRARIES = {
    "derivative": ("https://osf.io/download/2qbkt/", "derivative.rds"),
    "nobaseline": ("https://osf.io/download/jy7zk/", "nobaseline.rds"),
    "raw": ("https://osf.io/download/kzv3n/", "raw.rds"),
    "medoid_derivative": ("https://osf.io/download/2dmwu/", "medoid_derivative.rds"),
    "medoid_nobaseline": ("https://osf.io/download/8f3sg/", "medoid_nobaseline.rds"),
    "model_derivative": ("https://osf.io/download/s5bmh/", "model_derivative.rds"),
    "model_nobaseline": ("https://osf.io/download/v4abf/", "model_nobaseline.rds"),
}
OPENSPECY_BAND_RECIPE_VERSION = "arpls-savgol-ramanchada2-topo-normalized-v1"


class OpenSpecyDecoderUnavailable(ImportError):
    pass


class OpenSpecyArchiveError(ValueError):
    pass


_READER_HINT = "Install rdata in the IR/Raman Python environment to read local OpenSpecy RDS archives."


@dataclass(frozen=True, slots=True)
class OpenSpecyLibraryInfo:
    key: str
    url: str
    filename: str
    path: Path

    @property
    def label(self) -> str:
        return self.key.replace("_", " ")

    @property
    def is_cached(self) -> bool:
        return self.path.exists()


class OpenSpecyLibrarySource:
    name = "OpenSpecy"
    band_recipe_version = OPENSPECY_BAND_RECIPE_VERSION

    def __init__(self, cache_root: str | Path | None = None) -> None:
        self.cache_root = Path(cache_root) if cache_root is not None else app_cache_dir() / "openspecy"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.reference_cache = ReferenceSpectrumCache(self.cache_root)
        self._records: list[CandidateRecord] = []
        self._decoder_error = ""
        self._rds_cache: dict[tuple[Path, str], dict] = {}
        self._rds_failures: dict[tuple[Path, str], tuple[type[Exception], str]] = {}
        self._ssl_context = self._create_ssl_context()
        self.refresh_index()

    def available_libraries(self) -> list[OpenSpecyLibraryInfo]:
        return [
            OpenSpecyLibraryInfo(
                key=key,
                url=url,
                filename=filename,
                path=self.cache_root / filename,
            )
            for key, (url, filename) in OPENSPECY_LIBRARIES.items()
        ]

    def cached_library_keys(self) -> list[str]:
        return [info.key for info in self.available_libraries() if info.is_cached]

    def download_library(self, key: str = "derivative") -> Path:
        libraries = {info.key: info for info in self.available_libraries()}
        if key not in libraries:
            raise ValueError(f"Unknown OpenSpecy library: {key}")
        info = libraries[key]
        info.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = info.path.with_suffix(info.path.suffix + ".part")
        request_kwargs = {"timeout": 120}
        if self._ssl_context is not None:
            request_kwargs["context"] = self._ssl_context
        try:
            with urllib.request.urlopen(info.url, **request_kwargs) as response, tmp_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            checkpoint()
            if info.path.is_file() and filecmp.cmp(tmp_path, info.path, shallow=False):
                return info.path
            tmp_path.replace(info.path)
            self.refresh_index(updated_library=key)
        finally:
            tmp_path.unlink(missing_ok=True)
        return info.path

    @staticmethod
    def _remove_library_files(info: OpenSpecyLibraryInfo) -> None:
        info.path.unlink(missing_ok=True)
        info.path.with_suffix(info.path.suffix + ".part").unlink(missing_ok=True)
        profile_cache_path(info.path).unlink(missing_ok=True)

    def clear_package(self, key: str) -> None:
        for info in self.available_libraries():
            if info.key == key:
                self._remove_library_files(info)
                self.refresh_index()
                return
        raise ValueError(f"Unknown OpenSpecy library: {key}")

    def clear(self) -> None:
        for info in self.available_libraries():
            self._remove_library_files(info)
        self._records = []
        self._decoder_error = ""
        self._rds_cache = {}
        self._rds_failures = {}
        self.reference_cache.clear_source(self.name)

    def status_row(self) -> list[str]:
        cached = self.cached_library_keys()
        size = sum(info.path.stat().st_size for info in self.available_libraries() if info.path.exists())
        if self._records:
            status = "Indexed"
            detail = f"{len(self._records)} spectra; {self.indexed_band_count()} line-indexed; {', '.join(cached)}"
        elif cached:
            status = "Downloaded"
            detail = f"{', '.join(cached)}; not indexed"
        else:
            status = "External"
            detail = "Open spectral library; use Download / update to cache .rds files"
        missing_reader = any(find_spec(module) is None for module in ("rdata", "xarray"))
        if cached and (reader_error := self._decoder_error or (_READER_HINT if missing_reader else "")):
            # A metadata index alone does not mean the archived profiles are readable.
            status = "Reader unavailable" if not self.indexed_band_count() else "Bands only"
            detail = f"{reader_error} {detail}"
        return [
            self.name,
            status,
            detail,
            str(len(self._records)),
            f"{size / (1024 * 1024):.1f} MB",
            str(self.cache_root),
        ]

    def raman_status_row(self) -> list[str]:
        row = self.status_row()
        count = self.reference_cache.indexed_count(kind=SignalKind.RAMAN, source=self.name)
        if self._records and row[1] not in {"Reader unavailable", "Bands only"}:
            row[1] = "Indexed" if count else "Empty"
            row[2] = f"{count} Raman cards; {', '.join(self.cached_library_keys()) or 'no archives cached'}"
        row[3] = str(count)
        return row

    def refresh_index(self, *, force: bool = False, updated_library: str | None = None) -> None:
        self._records = []
        self._decoder_error = ""
        self._rds_cache = {}
        self._rds_failures = {}
        cached = [info for info in self.available_libraries() if info.is_cached]
        if not cached:
            self.reference_cache.clear_source(self.name)
            return
        cached_keys = {info.key for info in cached}
        indexed_records = self.reference_cache.search(SourceQuery(), sources=[self.name], limit=-1)
        indexed_keys = {str(record.metadata.get("library", "")) for record in indexed_records}
        indexed_keys.discard("")
        if not force and updated_library is None and indexed_records and indexed_keys == cached_keys:
            self._records = indexed_records
            return
        previous = {}
        for record in indexed_records:
            previous.setdefault(record.metadata.get("library", ""), []).append(record)
        replaced_keys = {record.key for key, records in previous.items() if key not in cached_keys for record in records}
        rebuilt = []
        for info in cached:
            checkpoint()
            old_records = previous.get(info.key, [])
            if not force and info.key != updated_library and old_records:
                self._records.extend(old_records)
                continue
            try:
                fresh_records = self._records_from_rds(info)
                if not fresh_records:
                    raise OpenSpecyArchiveError(f"No readable spectra metadata in {info.filename}")
            except JobCancelled:
                raise
            except Exception as exc:
                self._decoder_error = str(exc)
                self._records.extend(old_records)
            else:
                self._records.extend(fresh_records)
                rebuilt.extend(fresh_records)
                replaced_keys.update(record.key for record in old_records)
        stale = sorted(replaced_keys)
        for i in range(0, len(stale), 400):
            self.reference_cache.delete_keys(stale[i:i + 400])
        if rebuilt:
            self.reference_cache.upsert_records(rebuilt)

    def _records_from_rds(self, info: OpenSpecyLibraryInfo) -> list[CandidateRecord]:
        stamp = archive_stamp(info.path)
        result = self._load_rds(info.path)
        records: list[CandidateRecord] = []
        if self._is_openspecy_object(result):
            write_profiles(info.path, stamp, result["wavenumber"], result["spectra"])
            metadata = result["metadata"]
            columns = {str(column).lower(): column for column in metadata.columns}
            sample_column = _first_column(columns, ["sample_name", "col_id"])
            id_column = _first_column(columns, ["spectrumid", "library_id", "object_id", "id", "sample_name"])
            name_column = _first_column(columns, ["spectrum_identity", "spectrumidentity", "material", "sample_name"])
            formula_column = _first_column(columns, ["ideal_chemistry", "molform", "formula", "chemical_formula"])
            type_column = _first_column(columns, ["spectrum_type", "spectrumtype", "type"])
            class_column = _first_column(columns, ["material_class", "librarytype", "source_class"])
            source_column = _first_column(columns, ["organization", "source", "citation"])
            if sample_column is None or id_column is None or name_column is None:
                return records
            for index, row in metadata.iterrows():
                spectrum_type = _clean_value(row.get(type_column, "") if type_column is not None else "")
                kind = self._kind_from_text(spectrum_type)
                entry_id = _clean_value(row.get(id_column, f"{info.key}-{index}"))
                sample_name = _clean_value(row.get(sample_column, entry_id))
                name = _clean_value(row.get(name_column, entry_id)) or entry_id
                formula = _clean_value(row.get(formula_column, "") if formula_column is not None else "")
                material_class = _clean_value(row.get(class_column, "") if class_column is not None else "")
                source_text = _clean_value(row.get(source_column, "") if source_column is not None else "")
                records.append(
                    CandidateRecord(
                        key=f"OpenSpecy:{info.key}:{sample_name}:{kind.value}",
                        source=self.name,
                        entry_id=entry_id,
                        name=name,
                        formula=formula,
                        kind=kind,
                        metadata={
                            "library": info.key,
                            "sample_name": sample_name,
                            "material_class": material_class,
                            "description": source_text,
                            "path": str(info.path),
                            "quality": "OpenSpecy reference",
                        },
                    )
                )
            return records

        for object_name, frame in result.items():
            if not hasattr(frame, "iterrows"):
                continue
            records.extend(self._records_from_frame(info, object_name, frame))
        return records

    def _records_from_frame(self, info: OpenSpecyLibraryInfo, object_name: object, frame) -> list[CandidateRecord]:
        records: list[CandidateRecord] = []
        columns = {str(column).lower(): column for column in frame.columns}
        id_column = _first_column(columns, ["library_id", "object_id", "id", "spectrumid"])
        name_column = _first_column(columns, ["spectrumidentity", "spectrum_identity", "sample_name", "name", "material"])
        formula_column = _first_column(columns, ["formula", "chemical_formula", "ideal_chemistry", "molform"])
        type_column = _first_column(columns, ["spectrumtype", "spectrum_type", "spectype", "type"])
        if id_column is None or name_column is None:
            return records
        for index, row in frame.iterrows():
            spectrum_type = _clean_value(row.get(type_column, "") if type_column is not None else "")
            kind = self._kind_from_text(spectrum_type)
            entry_id = _clean_value(row.get(id_column, f"{object_name}-{index}"))
            name = _clean_value(row.get(name_column, entry_id))
            formula = _clean_value(row.get(formula_column, "") if formula_column is not None else "")
            records.append(
                CandidateRecord(
                    key=f"OpenSpecy:{info.key}:{entry_id}:{kind.value}",
                    source=self.name,
                    entry_id=entry_id,
                    name=name,
                    formula=formula,
                    kind=kind,
                    metadata={
                        "library": info.key,
                        "object": str(object_name),
                        "sample_name": entry_id,
                        "path": str(info.path),
                        "quality": "OpenSpecy reference",
                    },
                )
            )
        return records

    def search(self, query: SourceQuery) -> list[CandidateRecord]:
        return self.reference_cache.search(query, sources=[self.name])

    def load_spectrum(self, candidate: CandidateRecord) -> ReferenceSpectrum:
        library = OPENSPECY_LIBRARIES.get(candidate.metadata.get("library", ""))
        path = self.cache_root / library[1] if library else Path(candidate.metadata.get("path", ""))
        if not path.is_file():
            raise FileNotFoundError(str(path))
        sample_name = candidate.metadata.get("sample_name", "")
        stamp = archive_stamp(path)
        cached = read_profile(path, stamp, sample_name) if library else None
        if cached is not None:
            x, y = cached
        else:
            result = self._load_rds(path)
            if not self._is_openspecy_object(result):
                raise NotImplementedError("OpenSpecy RDS object does not expose wavenumber/spectra arrays.")
            spectra = result["spectra"]
            if sample_name not in spectra.columns:
                raise KeyError(f"OpenSpecy spectrum column not found: {sample_name}")
            x = np.asarray(result["wavenumber"], dtype=float).reshape(-1)
            y = np.asarray(spectra[sample_name], dtype=float).reshape(-1)
            if library and x.size == y.size:
                write_profiles(path, stamp, x, spectra)
        if x.size != y.size:
            raise OpenSpecyArchiveError("OpenSpecy spectrum and wavenumber axis have different lengths")
        finite = np.isfinite(x) & np.isfinite(y)
        x, y = x[finite], y[finite]
        if not x.size:
            raise ValueError("OpenSpecy spectrum contains no numeric x/y values")
        order = np.argsort(x, kind="stable")
        spectrum = ReferenceSpectrum(
            x=x[order].tolist(),
            y=y[order].tolist(),
            kind=candidate.kind,
            name=candidate.name,
            source_path=str(path),
            record=candidate,
            band_set=self.reference_cache.load_band_set(candidate.key, OPENSPECY_BAND_RECIPE_VERSION),
        )
        return spectrum

    def build_raman_band_index(self) -> tuple[int, int]:
        return self.build_band_index(kind=SignalKind.RAMAN)

    def build_band_index(self, *, kind: SignalKind | None = None) -> tuple[int, int]:
        from vibrational_finder.preprocessing import PreprocessingOptions, preprocess_spectrum

        if not self._records and self._decoder_error:
            raise OpenSpecyArchiveError(self._decoder_error)
        indexed_keys = self.reference_cache.indexed_band_keys(self.name, OPENSPECY_BAND_RECIPE_VERSION)
        indexed = 0
        skipped = 0
        pending: list[tuple[str, ReferenceBandSet]] = []
        preprocessing_options = PreprocessingOptions(
            baseline_method="arpls",
            smoothing_window=7,
            smoothing_method="savgol",
            despike=True,
            normalize="max",
        )
        detection_options = BandDetectionOptions(
            min_prominence=0.04,
            min_distance_cm1=8.0,
            max_bands=80,
            backend="auto",
            fit_peaks=False,
        )
        for record in self._records:
            checkpoint()
            if record.key in indexed_keys or (kind is not None and record.kind != kind):
                continue
            try:
                reference = self.load_spectrum(record)
                processed = preprocess_spectrum(reference, preprocessing_options)
                band_set = extract_reference_band_set(
                    processed,
                    detection_options,
                    origin=str(record.metadata.get("origin") or "experimental"),
                )
                band_set.processing_recipe.update(
                    {
                        "baseline_method": "arpls",
                        "smoothing_method": "savgol",
                        "smoothing_window": 7,
                        "despike": True,
                        "normalization": "max",
                    }
                )
                if not band_set.bands:
                    skipped += 1
                    continue
                pending.append((record.key, band_set))
                if len(pending) >= 100:
                    self.reference_cache.upsert_band_sets(pending, OPENSPECY_BAND_RECIPE_VERSION)
                    pending.clear()
                indexed += 1
            except JobCancelled:
                raise
            except Exception:
                skipped += 1
        if pending:
            self.reference_cache.upsert_band_sets(pending, OPENSPECY_BAND_RECIPE_VERSION)
        return indexed, skipped

    def indexed_band_count(self) -> int:
        return self.reference_cache.indexed_band_reference_count(self.name, OPENSPECY_BAND_RECIPE_VERSION)

    def clear_band_index(self) -> None:
        self.reference_cache.clear_band_index(self.name)

    def load_candidates(
        self,
        query: SourceQuery | None = None,
        *,
        observed: ReferenceSpectrum | None = None,
        observed_bands: list[SpectralBand] | None = None,
        limit: int = 80,
    ) -> list[CompoundCandidate]:
        query = query or SourceQuery()
        if observed is not None and self.indexed_band_count() > 0:
            if observed_bands is None:
                observed_bands = detect_bands(
                    observed,
                    BandDetectionOptions(min_prominence=0.04, max_bands=60, backend="auto", fit_peaks=False),
                )
            strongest_observed = sorted(observed_bands, key=lambda band: band.intensity, reverse=True)[:24]
            records = self.reference_cache.search_by_bands(
                query,
                [band.position for band in strongest_observed],
                tolerance_cm1=20.0,
                sources=[self.name],
                recipe_version=OPENSPECY_BAND_RECIPE_VERSION,
                limit=max(limit * 4, 200),
            )
            if len(records) < limit:
                selected_keys = {record.key for record in records}
                records.extend(record for record in self.search(query) if record.key not in selected_keys)
            records = records[:limit]
        else:
            records = self.search(query)[:limit]
        candidates: list[CompoundCandidate] = []
        for record in records:
            try:
                reference = self.load_spectrum(record)
            except Exception:
                reference = None
            candidates.append(
                CompoundCandidate(
                    key=record.key,
                    source=record.source,
                    entry_id=record.entry_id,
                    name=record.name,
                    formula=record.formula,
                    kind=record.kind,
                    metadata=dict(record.metadata),
                    reference=reference,
                    band_set=reference.band_set if reference is not None else None,
                )
            )
        return candidates

    def _create_ssl_context(self):
        try:
            import certifi
        except Exception:
            return None
        return ssl.create_default_context(cafile=certifi.where())

    def _load_rds(self, path: Path) -> dict:
        cache_key = (path, archive_stamp(path))
        if cache_key in self._rds_cache:
            return self._rds_cache[cache_key]
        if cache_key in self._rds_failures:
            error_type, message = self._rds_failures[cache_key]
            raise error_type(message)
        failures = []
        try:
            import rdata
        except ImportError as exc:
            missing_rdata = str(exc)
        else:
            missing_rdata = ""
            try:
                result = self._read_with_rdata(rdata, path)
                if isinstance(result, dict) and result:
                    self._rds_cache[cache_key] = result
                    return result
                failures.append("rdata: unsupported RDS object")
            except Exception as exc:
                failures.append(f"rdata: {exc}")
        try:
            import pyreadr  # type: ignore[import-not-found]

            result = dict(pyreadr.read_r(str(path)))
            if result:
                self._rds_cache[cache_key] = result
                return result
            failures.append("pyreadr: no readable tables")
        except Exception as exc:
            failures.append(f"pyreadr: {exc}")
        if missing_rdata:
            error_type = OpenSpecyDecoderUnavailable
            message = f"{_READER_HINT} ({missing_rdata})"
        else:
            error_type = OpenSpecyArchiveError
            message = f"Could not read {path.name}: {'; '.join(failures)}"
        self._rds_failures[cache_key] = (error_type, message)
        raise error_type(message)

    @staticmethod
    def _read_with_rdata(rdata, path: Path):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r'Unknown encoding\. Assumed ASCII\.|Missing constructor for R class "(?:data\.table|OpenSpecy|list)"\..*',
                category=UserWarning,
                module=r"rdata\.conversion\._conversion",
            )
            return rdata.conversion.convert(rdata.parser.parse_file(path))

    def _is_openspecy_object(self, result: dict) -> bool:
        return all(key in result for key in ("wavenumber", "spectra", "metadata"))

    def _kind_from_text(self, text: str) -> SignalKind:
        lowered = text.lower()
        if "ftir" in lowered or lowered == "ir" or "infrared" in lowered:
            return SignalKind.FTIR
        if "raman" in lowered:
            return SignalKind.RAMAN
        return SignalKind.UNKNOWN


def _first_column(columns: dict[str, object], candidates: list[str]):
    for candidate in candidates:
        if candidate.lower() in columns:
            return columns[candidate.lower()]
    return None


def _clean_value(value: object) -> str:
    try:
        if value is None or value != value:
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if text in {"<NA>", "nan", "NaN", "None"}:
        return ""
    return text
