from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from zipfile import ZipFile
import json

from vibrational_finder.core.data_sources import SourceQuery
from vibrational_finder.core.models import SignalKind
from vibrational_finder.models import ReferenceBandSet, SpectralBand
from vibrational_finder.services.oulu_source import OuluRamanSource
from vibrational_finder.services.rruff_source import RRUFF_BAND_RECIPE_VERSION, RruffSource
from vibrational_finder.services.source_management import SpectroscopySourceManager
from vibrational_finder.ui.source_management_jobs import SourceManagementJobController


class SourceDownloadIndexTests(TestCase):
    def test_download_creates_disk_index_available_in_new_source(self):
        with TemporaryDirectory() as folder:
            db = OuluRamanSource(Path(folder))
            manager = SpectroscopySourceManager(SimpleNamespace(source=lambda _name: db))
            package = db.available_archives()[0]

            def download(key):
                self.assertEqual(key, package.key)
                with ZipFile(package.path, "w") as archive:
                    archive.writestr("crd.json", json.dumps([{
                        "mpid": "mp-1", "chemical_formula": "SiO2", "frequencies_cm": [240., 460.],
                        "Ramanactive": [1, 1], "IRlabels": ["A", "B"],
                    }]))
                return package.path

            with patch.object(db, "download_archive", side_effect=download):
                manager.update(db.name, package.key)

            self.assertTrue(db.index_path.is_file())
            reopened = OuluRamanSource(Path(folder))
            cards = reopened.search(SourceQuery(kind=SignalKind.RAMAN))
            self.assertEqual(len(cards), 1)
            self.assertEqual([band.position for band in reopened.load_spectrum(cards[0]).band_set.bands], [240., 460.])

    def test_download_failure_does_not_start_indexing(self):
        def download():
            raise OSError("offline")

        source = SimpleNamespace(
            download_library=download,
            build_band_index=lambda: self.fail("Indexing ran after a failed download"),
        )
        manager = SpectroscopySourceManager(SimpleNamespace(source=lambda _name: source))
        with self.assertRaisesRegex(OSError, "offline"):
            manager.update("fixture")

    def test_rruff_leaves_other_archive_indexed_and_invalidates_changed_archive(self):
        def archive_bytes(name, marker):
            payload = BytesIO()
            with ZipFile(payload, "w") as archive:
                archive.writestr(f"{name}__R000001.txt", f"##NAMES={name}\n100,0\n{marker},1\n500,0\n")
            return payload.getvalue()

        with TemporaryDirectory() as folder:
            root = Path(folder)
            original = archive_bytes("First", 300)
            (root / "excellent_unoriented.zip").write_bytes(original)
            (root / "fair_unoriented.zip").write_bytes(archive_bytes("Second", 400))
            source = RruffSource(root)
            keys = {record.metadata["archive"]: record.key for record in source.search(SourceQuery())}
            source.reference_cache.upsert_band_sets(
                [(key, ReferenceBandSet([SpectralBand(300., 1.)])) for key in keys.values()], RRUFF_BAND_RECIPE_VERSION,
            )
            with patch.object(source, "_ssl_context", return_value=None):
                with patch("urllib.request.urlopen", return_value=BytesIO(original)):
                    source.download_archive("excellent_unoriented")
                self.assertEqual(source.indexed_band_count(), 2)

                with patch("urllib.request.urlopen", return_value=BytesIO(archive_bytes("First", 350))):
                    source.download_archive("excellent_unoriented")
                self.assertEqual(source.indexed_count(), 2)
                self.assertEqual(source.indexed_band_count(), 1)
                self.assertIsNotNone(source.reference_cache.load_band_set(keys["fair_unoriented"]))

            manager = SpectroscopySourceManager(SimpleNamespace(source=lambda _name: source))
            manager.clear(source.name, "excellent_unoriented")
            self.assertEqual(source.cached_archive_keys(), ["fair_unoriented"])
            self.assertEqual(source.indexed_count(), 1)
            self.assertEqual(source.indexed_band_count(), 1)

    def test_rruff_http_403_offers_selected_archive_and_imports_local_zip(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = RruffSource(root / "cache")
            manager = SpectroscopySourceManager(SimpleNamespace(source=lambda _name: source))
            package = source.available_archives()[2]
            local_zip = root / package.path.name
            with ZipFile(local_zip, "w") as archive:
                archive.writestr("Quartz__R000001.txt", "##NAMES=Quartz\n100,0\n300,1\n500,0\n")
            failure = HTTPError(package.url, 403, "Forbidden", {}, None)

            with patch("urllib.request.urlopen", side_effect=failure):
                result = SourceManagementJobController._run(manager, source.name, "update", package.key)
            self.assertEqual(result.archive.key, package.key)
            self.assertEqual(result.archive.url, package.url)
            self.assertFalse(package.is_cached)

            with patch.object(source, "build_band_index", return_value=(1, 0)) as build:
                result = manager.import_archive(source.name, package.key, local_zip)
            build.assert_called_once()
            self.assertEqual(result.index_counts, (1, 0))
            self.assertEqual(source.indexed_count(), 1)
            self.assertEqual(package.path.read_bytes(), local_zip.read_bytes())

            invalid = root / "invalid.zip"
            invalid.write_text("Access denied", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "not a valid ZIP"):
                manager.import_archive(source.name, package.key, invalid)
            self.assertEqual(package.path.read_bytes(), local_zip.read_bytes())
            self.assertEqual(source.indexed_count(), 1)
