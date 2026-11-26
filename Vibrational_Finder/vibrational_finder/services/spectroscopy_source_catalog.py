from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from vibrational_finder.cancellation import JobCancelled, checkpoint
from vibrational_finder.core.cache import app_cache_dir
from vibrational_finder.core.data_sources import SourceQuery
from vibrational_finder.core.models import CandidateRecord, SignalKind

from vibrational_finder.models import CompoundCandidate, ReferenceSpectrum
from vibrational_finder.project_state import ProjectUiState, VibrationalProjectState
from vibrational_finder.services.editable_reference import USER_LIBRARY_NAME, open_user_reference_library
from vibrational_finder.services.folder_library import FolderLibrarySource
from vibrational_finder.services.reference_cache import ReferenceSpectrumCache
from vibrational_finder.services.user_library import UserLibrarySource

if TYPE_CHECKING:
    from vibrational_finder.services.rest_library_source import RestConnection


SourceFactory = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    name: str
    kinds: tuple[SignalKind, ...]
    factory: SourceFactory
    path: str = ""
    user_managed: bool = False
    default_enabled: bool = False
    website: str = ""
    remote_url: str = ""
    rest_connection: RestConnection | None = None
    local_cache_subdir: str = ""

    @property
    def preference_key(self) -> str | None:
        """Identify workspace sources separately from transient project references."""
        if self.remote_url:
            kind = "rest" if self.rest_connection is not None else "catalog"
            return f"{kind}:{self.remote_url}"
        if self.name == ProjectReferenceSource.name or (self.user_managed and self.name != USER_LIBRARY_NAME):
            return None
        return f"builtin:{self.name}"


@dataclass(frozen=True, slots=True)
class CatalogSourceHandle:
    catalog: SpectroscopySourceCatalog
    descriptor: SourceDescriptor

    @property
    def name(self) -> str:
        return self.descriptor.name

    def open(self) -> Any:
        checkpoint()
        if self.catalog._descriptors.get(self.name) is not self.descriptor:
            raise RuntimeError(f"{self.name} was removed or reconfigured")
        source = self.catalog.source(self.name)
        checkpoint()
        return source


@dataclass(frozen=True, slots=True)
class SourceStatus:
    name: str
    enabled: bool
    kinds: tuple[SignalKind, ...]
    state: str
    count: int = 0
    detail: str = ""
    path: str = ""
    website: str = ""


class ProjectReferenceSource:
    name = "Project references"

    def __init__(self, project: VibrationalProjectState) -> None:
        self.project = project

    def search(self, query: SourceQuery) -> list[CandidateRecord]:
        text = query.text.strip().lower()
        records: list[CandidateRecord] = []
        for record in self.project.reference_records:
            if query.kind != SignalKind.UNKNOWN and record.kind not in {query.kind, SignalKind.UNKNOWN}:
                continue
            haystack = " ".join((record.name, record.formula, record.entry_id, record.source)).lower()
            if text and text not in haystack:
                continue
            records.append(record)
        return records

    def load_candidates(self, query: SourceQuery | None = None, **_kwargs: object) -> list[CompoundCandidate]:
        references = {
            reference.record.key: reference
            for reference in self.project.reference_spectra
            if reference.record is not None
        }
        return [
            CompoundCandidate(
                key=record.key,
                source=record.source,
                entry_id=record.entry_id,
                name=record.name,
                formula=record.formula,
                kind=record.kind,
                metadata=dict(record.metadata),
                reference=references.get(record.key),
                band_set=references[record.key].band_set if record.key in references else None,
            )
            for record in self.search(query or SourceQuery())
        ]

    def load_spectrum(self, candidate: CandidateRecord) -> ReferenceSpectrum:
        for reference in self.project.reference_spectra:
            if reference.record is not None and reference.record.key == candidate.key:
                return reference
        raise ValueError("This project entry has no reference spectrum")


def default_source_descriptors() -> list[SourceDescriptor]:
    from vibrational_finder.services.openspecy_source import OpenSpecyLibrarySource
    from vibrational_finder.services.oulu_source import OULU_RECORD_URL, OuluRamanSource
    from vibrational_finder.services.research_raman_source import MlrodSource, RamanBenchSource
    from vibrational_finder.services.rod_source import RodSource
    from vibrational_finder.services.rruff_source import RRUFF_DOWNLOAD_PAGE, RruffSource
    from vibrational_finder.services.wurm_source import WURM_CATALOG_URL, WURM_NAME, WurmSource

    return [
        SourceDescriptor(
            USER_LIBRARY_NAME, (SignalKind.RAMAN,), open_user_reference_library,
            user_managed=True, default_enabled=True,
        ),
        SourceDescriptor("RRUFF", (SignalKind.RAMAN,), RruffSource, default_enabled=True,
                         website=RRUFF_DOWNLOAD_PAGE, local_cache_subdir="rruff"),
        SourceDescriptor("ROD", (SignalKind.RAMAN,), RodSource, default_enabled=True, local_cache_subdir="rod"),
        SourceDescriptor("OpenSpecy", (SignalKind.RAMAN,), OpenSpecyLibrarySource,
                         default_enabled=True, local_cache_subdir="openspecy"),
        SourceDescriptor("Oulu CRD", (SignalKind.RAMAN,), OuluRamanSource, website=OULU_RECORD_URL, local_cache_subdir="oulu_crd"),
        SourceDescriptor(WURM_NAME, (SignalKind.RAMAN,), WurmSource, website=WURM_CATALOG_URL, local_cache_subdir="wurm"),
        SourceDescriptor(
            "MLROD", (SignalKind.RAMAN,), MlrodSource,
            website="https://ahed.nasa.gov/datasets/f5b6051bfeb18c5a7eaef6504582", local_cache_subdir="mlrod",
        ),
        SourceDescriptor(
            "RamanBench", (SignalKind.RAMAN,), RamanBenchSource,
            website="https://github.com/ml-lab-htw/RamanBench", local_cache_subdir="ramanbench",
        ),
    ]


@dataclass(slots=True)
class SpectroscopySourceCatalog:
    ui_state: ProjectUiState
    project: VibrationalProjectState | None = None
    descriptors: Iterable[SourceDescriptor] | None = None
    _descriptors: dict[str, SourceDescriptor] = field(init=False, default_factory=dict)
    _instances: dict[str, Any] = field(init=False, default_factory=dict)
    _failures: dict[str, str] = field(init=False, default_factory=dict)
    _refresh_revisions: dict[str, int] = field(init=False, default_factory=dict)
    _defaulted_sources: set[str] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        initial = default_source_descriptors() if self.descriptors is None else list(self.descriptors)
        for descriptor in initial:
            self._register(descriptor)
        if self.project is not None and (self.project.reference_records or self.project.reference_spectra):
            self._register(SourceDescriptor(
                ProjectReferenceSource.name,
                (SignalKind.RAMAN,),
                lambda: ProjectReferenceSource(self.project),
                default_enabled=True,
            ))

    def _register(self, descriptor: SourceDescriptor, instance: Any | None = None) -> str:
        self._descriptors[descriptor.name] = descriptor
        if instance is not None:
            self._instances[descriptor.name] = instance
        if descriptor.name not in self.ui_state.enabled_sources:
            self.ui_state.enabled_sources[descriptor.name] = descriptor.default_enabled
            self._defaulted_sources.add(descriptor.name)
        return descriptor.name

    def names(self) -> list[str]:
        return [name for name, descriptor in self._descriptors.items() if SignalKind.RAMAN in descriptor.kinds]

    def for_project(self, project: VibrationalProjectState) -> "SpectroscopySourceCatalog":
        descriptors = [
            descriptor
            for name, descriptor in self._descriptors.items()
            if name != ProjectReferenceSource.name
        ]
        catalog = SpectroscopySourceCatalog(project.ui, project=project, descriptors=descriptors)
        for name, instance in self._instances.items():
            if name in catalog._descriptors and catalog._descriptors[name].user_managed:
                catalog._instances[name] = instance
        return catalog

    def descriptor(self, name: str) -> SourceDescriptor:
        return self._descriptors[name]

    def apply_enabled_preferences(self, preferences: Mapping[str, bool]) -> None:
        """Use workspace defaults only where the project has no explicit selection."""
        for name in self._defaulted_sources:
            key = self._descriptors[name].preference_key
            if key is not None and key in preferences:
                self.ui_state.enabled_sources[name] = preferences[key]

    def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._descriptors:
            raise KeyError(name)
        self.ui_state.enabled_sources[name] = bool(enabled)
        self._defaulted_sources.discard(name)

    def _instance(self, name: str) -> Any | None:
        instance = self._instances.get(name)
        if instance is not None:
            return instance
        if name in self._failures:
            return None
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            return None
        revision = self._refresh_revisions.get(name, 0)
        try:
            instance = descriptor.factory()
        except JobCancelled:
            raise
        except Exception as exc:
            if self._descriptors.get(name) is descriptor and revision == self._refresh_revisions.get(name, 0):
                self._failures[name] = f"{type(exc).__name__}: {exc}"
            return None
        if self._descriptors.get(name) is not descriptor or revision != self._refresh_revisions.get(name, 0):
            return None
        return self._instances.setdefault(name, instance)

    def source(self, name: str) -> Any:
        """Return one lazily constructed adapter through a stable public boundary."""
        if name not in self._descriptors:
            raise KeyError(name)
        source = self._instance(name)
        if source is None:
            raise RuntimeError(self._failures.get(name, f"{name} is unavailable"))
        return source

    def enabled_sources(self, kind: SignalKind = SignalKind.UNKNOWN) -> list[Any]:
        if kind not in {SignalKind.RAMAN, SignalKind.UNKNOWN}:
            return []
        sources: list[Any] = []
        for name, descriptor in self._descriptors.items():
            if not self.ui_state.enabled_sources.get(name, False):
                continue
            if SignalKind.RAMAN not in descriptor.kinds:
                continue
            source = self._instance(name)
            if source is not None:
                sources.append(source)
        return sources

    def enabled_handles(self, kind: SignalKind = SignalKind.UNKNOWN) -> tuple[CatalogSourceHandle, ...]:
        """Capture the selected source identities without touching files or databases."""
        if kind not in {SignalKind.RAMAN, SignalKind.UNKNOWN}:
            return ()
        return tuple(
            CatalogSourceHandle(self, descriptor)
            for name, descriptor in self._descriptors.items()
            if self.ui_state.enabled_sources.get(name, False) and SignalKind.RAMAN in descriptor.kinds
        )

    def local_reference_card_count(self, kind: SignalKind) -> int:
        """Information only: enabled, locally known cards before chemical/text filters."""
        if kind not in {SignalKind.RAMAN, SignalKind.UNKNOWN}:
            return 0
        kind = SignalKind.RAMAN
        count = 0
        query = SourceQuery(kind=kind)
        for name, descriptor in self._descriptors.items():
            if not self.ui_state.enabled_sources.get(name, False):
                continue
            if kind != SignalKind.UNKNOWN and kind not in descriptor.kinds:
                continue
            source = self._instances.get(name)
            if name == USER_LIBRARY_NAME:
                count += len((source or open_user_reference_library()).search(query))
            elif name == ProjectReferenceSource.name and self.project is not None:
                count += len(ProjectReferenceSource(self.project).search(query))
            elif isinstance(source, (FolderLibrarySource, UserLibrarySource)):
                count += len(source.search(query))
            else:
                cache = getattr(source, "reference_cache", None)
                if isinstance(cache, ReferenceSpectrumCache):
                    index_path = cache.index_path
                    name = source.name
                elif descriptor.local_cache_subdir:
                    index_path = app_cache_dir() / descriptor.local_cache_subdir / "reference_index.sqlite"
                else:
                    continue
                count += ReferenceSpectrumCache.count_existing_records(index_path, source=name, kind=kind)
        return count

    def _source_count(self, source: Any) -> int:
        cache = getattr(source, "reference_cache", None) if getattr(source, "band_recipe_version", "") else None
        if isinstance(cache, ReferenceSpectrumCache):
            return ReferenceSpectrumCache.count_existing_records(cache.index_path, source=source.name, kind=SignalKind.RAMAN)
        indexed_count = getattr(source, "indexed_count", None)
        if callable(indexed_count):
            return int(indexed_count())
        if hasattr(source, "search"):
            return len(source.search(SourceQuery(kind=SignalKind.RAMAN)))
        if hasattr(source, "load_candidates"):
            return len(source.load_candidates(SourceQuery(kind=SignalKind.RAMAN)))
        return 0

    def _status(self, name: str, descriptor: SourceDescriptor) -> SourceStatus:
        enabled = self.ui_state.enabled_sources.get(name, False)
        source = self._instance(name)
        if source is None:
            return SourceStatus(
                name, enabled, descriptor.kinds, "failed", detail=self._failures.get(name, "Unavailable"),
                path=descriptor.path, website=descriptor.website,
            )
        try:
            count = self._source_count(source)
            detail = ""
            path = descriptor.path
            raw_state = ""
            if hasattr(source, "status_row"):
                raman_status = getattr(source, "raman_status_row", source.status_row)
                row = raman_status()
                raw_state = str(row[1]).lower() if len(row) > 1 else ""
                detail = str(row[2]) if len(row) > 2 else ""
                if len(row) > 5:
                    path = str(row[5])
            if not path:
                for attribute in ("manifest_path", "folder_path", "cache_root", "path"):
                    value = getattr(source, attribute, "")
                    if value:
                        path = str(value)
                        break
            if raw_state == "reader unavailable":
                state = "reader unavailable"
            elif count:
                state = "ready"
            elif raw_state in {"external", "downloaded", "offline", "not cached"}:
                state = "offline"
            else:
                state = "empty"
            return SourceStatus(name, enabled, descriptor.kinds, state, count, detail, path, descriptor.website)
        except Exception as exc:
            self._failures[name] = f"{type(exc).__name__}: {exc}"
            return SourceStatus(
                name, enabled, descriptor.kinds, "failed", detail=self._failures[name],
                path=descriptor.path, website=descriptor.website,
            )

    def status_rows(self, names: Iterable[str] | None = None) -> list[SourceStatus]:
        selected = self.names() if names is None else names
        return [self._status(name, self._descriptors[name]) for name in selected]

    def refresh(self, name: str | None = None) -> None:
        names = [name] if name is not None else list(self._descriptors)
        for source_name in names:
            self._refresh_revisions[source_name] = self._refresh_revisions.get(source_name, 0) + 1
            self._failures.pop(source_name, None)
            if source_name == USER_LIBRARY_NAME:
                # A running search keeps its own consistent snapshot of the cards.
                self._instances.pop(source_name, None)
                continue
            source = self._instances.get(source_name)
            if source is not None and hasattr(source, "refresh_index"):
                try:
                    source.refresh_index()
                except Exception as exc:
                    self._failures[source_name] = f"{type(exc).__name__}: {exc}"

    def add_user_library(self, path: str | Path) -> str:
        source = UserLibrarySource(path)
        name = self._unique_name(f"User: {Path(path).stem}")
        source.name = name
        self._register(SourceDescriptor(name, self._detect_kinds(source), lambda: source, str(Path(path)), True), source)
        self.set_enabled(name, True)
        return name

    def add_folder_library(self, path: str | Path, *, library_type: str = "measured") -> str:
        source = FolderLibrarySource(path, library_type=library_type)
        name = self._unique_name(f"Folder: {Path(path).name}")
        source.name = name
        source.source_name = name
        self._register(SourceDescriptor(name, self._detect_kinds(source), lambda: source, str(Path(path)), True), source)
        self.set_enabled(name, True)
        return name

    def add_remote_library(self, url: str) -> str:
        from vibrational_finder.services.remote_catalog_source import RemoteCatalogSource, catalog_url, url_library_name

        url = catalog_url(url)
        for descriptor in self._descriptors.values():
            if descriptor.remote_url == url and descriptor.rest_connection is None:
                return descriptor.name
        name = self._unique_name(url_library_name(url))
        return self._register(SourceDescriptor(
            name, (SignalKind.RAMAN,), lambda: RemoteCatalogSource(name, url),
            path=url, user_managed=True, website=url, remote_url=url,
        ))

    def remote_library_urls(self) -> list[str]:
        return [
            entry.remote_url for entry in self._descriptors.values()
            if entry.remote_url and entry.rest_connection is None
        ]

    def add_rest_library(self, config: RestConnection) -> str:
        from vibrational_finder.services.rest_library_source import RestLibrarySource

        name = self._unique_name(config.source_name)
        for descriptor in self._descriptors.values():
            if descriptor.rest_connection is not None and descriptor.rest_connection.url == config.url:
                name = descriptor.name
                if descriptor.rest_connection == config:
                    return name
                self._instances.pop(name, None)
                self._failures.pop(name, None)
                break
        # Keep old IR-only connection definitions intact, but never activate them here.
        kinds = (SignalKind.RAMAN,) if config.kind_path else (SignalKind(config.default_kind),)
        return self._register(SourceDescriptor(
            name, kinds, lambda: RestLibrarySource(name, config), path=config.url, user_managed=True,
            remote_url=config.url, rest_connection=config,
        ))

    def rest_connections(self) -> list[RestConnection]:
        return [entry.rest_connection for entry in self._descriptors.values() if entry.rest_connection is not None]

    def remove_remote_library(self, name: str) -> None:
        if not self.descriptor(name).remote_url:
            raise ValueError("Only URL libraries can be disconnected here.")
        self._descriptors.pop(name)
        self._instances.pop(name, None)
        self._failures.pop(name, None)
        self.ui_state.enabled_sources.pop(name, None)
        self._defaulted_sources.discard(name)

    def _detect_kinds(self, source: Any) -> tuple[SignalKind, ...]:
        kinds = {record.kind for record in source.search(SourceQuery()) if record.kind != SignalKind.UNKNOWN}
        return (SignalKind.RAMAN,) if not kinds or SignalKind.RAMAN in kinds else ()

    def _unique_name(self, base: str) -> str:
        if base not in self._descriptors:
            return base
        index = 2
        while f"{base} ({index})" in self._descriptors:
            index += 1
        return f"{base} ({index})"

