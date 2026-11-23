from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vibrational_finder.cancellation import JobCancelled, checkpoint
from vibrational_finder.core.models import SignalKind
from vibrational_finder.services.spectroscopy_source_catalog import SpectroscopySourceCatalog


@dataclass(frozen=True, slots=True)
class SourcePackageOption:
    key: str
    label: str
    cached: bool = False


@dataclass(frozen=True, slots=True)
class SourceManagementCapabilities:
    can_update: bool = False
    can_clear: bool = False
    can_build_index: bool = False
    can_clear_index: bool = False
    packages: tuple[SourcePackageOption, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceUpdateResult:
    package: object
    index_counts: tuple[int, int] | None = None


class SpectroscopySourceManager:
    """Normalize cache lifecycle operations exposed by spectroscopy adapters."""

    def __init__(self, catalog: SpectroscopySourceCatalog) -> None:
        self.catalog = catalog

    def packages(self, name: str) -> list[SourcePackageOption]:
        source = self.catalog.source(name)
        raw_packages: list[Any]
        if hasattr(source, "available_archives"):
            raw_packages = list(source.available_archives())
        elif hasattr(source, "available_libraries"):
            raw_packages = list(source.available_libraries())
        else:
            return []
        return [
            SourcePackageOption(
                key=str(item.key),
                label=str(getattr(item, "label", item.key)),
                cached=bool(getattr(item, "is_cached", False)),
            )
            for item in raw_packages if getattr(item, "kind", SignalKind.RAMAN) == SignalKind.RAMAN
        ]

    def capabilities(self, name: str) -> SourceManagementCapabilities:
        source = self.catalog.source(name)
        packages = tuple(self.packages(name))
        return SourceManagementCapabilities(
            can_update=any(
                hasattr(source, method)
                for method in ("download_archive", "download_library", "download_metadata")
            ),
            can_clear=hasattr(source, "clear"),
            can_build_index=hasattr(source, "build_band_index"),
            can_clear_index=hasattr(source, "clear_band_index"),
            packages=packages,
        )

    def update(self, name: str, package_key: str | None = None) -> SourceUpdateResult:
        source = self.catalog.source(name)
        packages = self.packages(name)
        if package_key is not None and packages and package_key not in {item.key for item in packages}:
            raise ValueError(f"Unknown package for {name}: {package_key}")
        if hasattr(source, "download_library"):
            package = source.download_library(package_key) if package_key is not None else source.download_library()
        elif hasattr(source, "download_archive"):
            package = source.download_archive(package_key) if package_key is not None else source.download_archive()
        elif hasattr(source, "download_metadata"):
            if package_key is not None:
                raise ValueError(f"{name} does not provide selectable packages")
            package = source.download_metadata()
        else:
            raise RuntimeError(f"{name} does not support cache updates")

        return self._index_download(name, source, package)

    def import_archive(self, name: str, package_key: str, path: str | Path) -> SourceUpdateResult:
        source = self.catalog.source(name)
        if not hasattr(source, "import_archive"):
            raise RuntimeError(f"{name} does not support importing downloaded archives")
        return self._index_download(name, source, source.import_archive(package_key, path))

    def _index_download(self, name: str, source: Any, package: object) -> SourceUpdateResult:
        checkpoint()
        if not hasattr(source, "build_band_index"):
            return SourceUpdateResult(package)
        try:
            return SourceUpdateResult(package, self.build_index(name))
        except JobCancelled:
            raise
        except Exception as exc:
            raise RuntimeError(f"Download saved locally, but indexing failed: {exc}") from exc

    def clear(self, name: str, package_key: str | None = None) -> None:
        source = self.catalog.source(name)
        if not hasattr(source, "clear"):
            raise RuntimeError(f"{name} does not support cache clearing")
        if package_key is not None:
            packages = self.packages(name)
            if package_key not in {item.key for item in packages}:
                raise ValueError(f"Unknown package for {name}: {package_key}")
            if hasattr(source, "clear_package"):
                source.clear_package(package_key)
                return
            if len(packages) != 1:
                raise RuntimeError(f"{name} does not support deleting individual packages")
        source.clear()

    def build_index(self, name: str) -> tuple[int, int]:
        source = self.catalog.source(name)
        if not hasattr(source, "build_band_index"):
            raise RuntimeError(f"{name} does not support line indexing")
        raman_builder = getattr(source, "build_raman_band_index", source.build_band_index)
        result = raman_builder()
        return int(result[0]), int(result[1])

    def clear_index(self, name: str) -> None:
        source = self.catalog.source(name)
        if not hasattr(source, "clear_band_index"):
            raise RuntimeError(f"{name} does not support line-index clearing")
        source.clear_band_index()
