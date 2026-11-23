from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from vibrational_finder.core.app_paths import raman_app_root, raman_data_root, raman_logs_root


class RamanPathsTests(TestCase):
    def test_clean_profile_uses_raman_and_does_not_create_legacy_folder(self):
        with TemporaryDirectory() as folder:
            sci = Path(folder) / "Sci"
            self.assertEqual(raman_app_root(sci), sci / "apps" / "Raman Phase Finder")
            self.assertEqual(raman_logs_root(sci), sci / "logs" / "Raman Phase Finder")
            self.assertFalse(sci.exists())

    def test_upgrade_preserves_database_settings_and_matplotlib_cache(self):
        with TemporaryDirectory() as folder:
            sci = Path(folder)
            old = sci / "apps" / "ir_raman_analysis_toolkit"
            snapshots = {
                "data/cache/rruff/reference_index.sqlite": b"index",
                "data/settings/qt/IRRamanPhaseFinder/Standalone.ini": b"[General]\nkey=value",
                "matplotlib/font.cache": b"existing",
            }
            for relative, content in snapshots.items():
                path = old / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            new = raman_app_root(sci)

            self.assertFalse(old.exists())
            self.assertEqual(raman_app_root(sci), new)
            for relative, content in snapshots.items():
                path = new / relative
                if "IRRamanPhaseFinder" in relative:
                    path = new / relative.replace("IRRamanPhaseFinder", "Raman Phase Finder")
                self.assertEqual(path.read_bytes(), content)

    def test_existing_data_takes_precedence_without_discarding_legacy_data(self):
        with TemporaryDirectory() as folder:
            sci = Path(folder)
            old = sci / "apps" / "ir_raman_analysis_toolkit"
            new = sci / "apps" / "Raman Phase Finder"
            for root, content in [(old, b"older"), (new, b"current")]:
                (root / "data").mkdir(parents=True)
                (root / "data" / "card").write_bytes(content)
            (old / "matplotlib").mkdir()
            self.assertEqual(raman_app_root(sci), new)
            self.assertEqual((new / "data" / "card").read_bytes(), b"current")
            self.assertEqual((old / "data" / "card").read_bytes(), b"older")
            self.assertTrue((new / "matplotlib").is_dir())

    def test_short_name_migrates_current_data_without_touching_restored_backup(self):
        with TemporaryDirectory() as folder:
            sci = Path(folder)
            legacy = sci / "apps" / "ir_raman_analysis_toolkit"
            short = sci / "apps" / "raman"
            old_log = sci / "logs" / "raman"
            (legacy / "data").mkdir(parents=True)
            (legacy / "data" / "backup").write_bytes(b"restored")
            (short / "data" / "settings" / "qt" / "raman").mkdir(parents=True)
            (short / "data" / "settings" / "qt" / "raman" / "Standalone.ini").write_bytes(b"current")
            old_log.mkdir(parents=True)
            (old_log / "startup.log").write_bytes(b"log")

            new = raman_app_root(sci)
            self.assertEqual(new, sci / "apps" / "Raman Phase Finder")
            self.assertEqual((new / "data/settings/qt/Raman Phase Finder/Standalone.ini").read_bytes(), b"current")
            self.assertFalse(short.exists())
            self.assertEqual((legacy / "data" / "backup").read_bytes(), b"restored")
            self.assertEqual((raman_logs_root(sci) / "startup.log").read_bytes(), b"log")

    def test_explicit_data_override_leaves_legacy_directory_untouched(self):
        with TemporaryDirectory() as folder:
            given = Path(folder) / "override"
            with patch.dict("os.environ", {"IR_RAMAN_DATA_DIR": str(given)}):
                self.assertEqual(raman_data_root(), given)
