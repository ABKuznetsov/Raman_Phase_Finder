import os
import time
import unittest

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from vibrational_finder.core.models import CandidateRecord, SignalKind
from vibrational_finder.models import ObservedSpectrum, ReferenceBandSet, ReferenceSpectrum, SpectralBand
from vibrational_finder.project_state import VibrationalProjectState
from vibrational_finder.reference_card_work import load_reference_card
from vibrational_finder.services.spectroscopy_source_catalog import SourceDescriptor, SpectroscopySourceCatalog
from vibrational_finder.ui.analysis_windows import SpectroscopyFinderWindow


class LocalReferences:
    name = "Local test references"

    def __init__(self):
        self.positions = {"a": [200, 320, 480], "b": [740, 880]}
        self.records = [CandidateRecord(key, self.name, key, f"Phase {key}", "SiO2", SignalKind.RAMAN)
                        for key in self.positions]
        self.search_count = 0

    def search(self, _query):
        self.search_count += 1
        return self.records

    def load_spectrum(self, record):
        bands = [SpectralBand(position, 1.0, width=8.) for position in self.positions[record.key]]
        return ReferenceSpectrum(x=[], y=[], kind=SignalKind.RAMAN, band_set=ReferenceBandSet(bands))


class AutoGainTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def open_window(self, *, mixture):
        source = LocalReferences()
        x = np.linspace(100, 1000, 1801)
        peaks = [(200, 1.), (320, .9), (480, .8)]
        if mixture:
            peaks.extend([(740, .7), (880, .65)])
        y = sum(height * np.exp(-0.5 * ((x - position) / 3.5) ** 2) for position, height in peaks)
        spectrum = ObservedSpectrum(x=x.tolist(), y=y.tolist(), kind=SignalKind.RAMAN)
        project = VibrationalProjectState(raman_spectra=[spectrum], active_spectrum_key=spectrum.id)
        catalog = SpectroscopySourceCatalog(project.ui, project, [
            SourceDescriptor(source.name, (SignalKind.RAMAN,), lambda: source, default_enabled=True),
        ])
        window = SpectroscopyFinderWindow(project, defer_source_settings=True, source_catalog=catalog)
        self.addCleanup(window._shutdown_analysis_jobs)
        window._set_search_mode("auto")
        profile = window.accepted_components_controller.profile(spectrum.id)
        profile.results = [load_reference_card(source, source.records[0])]
        window._refresh_candidate_workspace()
        return window, profile, source

    def wait_for_gain(self, window):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not (window.residual_job_controller.is_running() or window.gain_table_job_controller.is_running()):
                return
            time.sleep(.01)
        self.fail("Gain did not finish")

    def test_double_click_finds_phase_outside_initial_shortlist(self):
        window, profile, source = self.open_window(mixture=True)
        candidates = window.candidate_workspace
        self.assertEqual(candidates.candidate_key_at(0), "a")
        candidates.table.cellDoubleClicked.emit(0, 2)
        self.assertTrue(candidates.score_mode_label.text().startswith("Gain"))
        self.assertNotEqual(candidates.candidate_key_at(0), "a")
        self.wait_for_gain(window)
        self.assertEqual(source.search_count, 1)
        self.assertEqual([row.candidate.key for row in profile.selected_results], ["a"])
        self.assertEqual([row.candidate.key for row in profile.residual_results], ["b"])
        self.assertEqual(candidates.candidate_key_at(0), "b")
        self.assertIsNotNone(profile.saved_gain)
        self.assertTrue(profile.saved_gain.ranked)
        candidates.table.cellDoubleClicked.emit(0, 2)
        self.wait_for_gain(window)
        self.assertEqual([row.candidate.key for row in profile.selected_results], ["a", "b"])
        self.assertEqual(candidates.table.rowCount(), 0)
        self.assertEqual(source.search_count, 1)
        window._clear_components()
        self.assertEqual(candidates.score_mode_label.text(), "Match")
        self.assertGreater(candidates.table.rowCount(), 0)

    def test_no_remaining_bands_keeps_gain_table_empty(self):
        window, profile, source = self.open_window(mixture=False)
        candidates = window.candidate_workspace
        candidates.table.cellDoubleClicked.emit(0, 2)
        self.wait_for_gain(window)
        self.assertEqual(source.search_count, 0)
        self.assertTrue(candidates.score_mode_label.text().startswith("Gain"))
        self.assertEqual(candidates.table.rowCount(), 0)
        self.assertIn("No unexplained bands", candidates.status_label.text())
        self.assertTrue(profile.saved_gain.ranked)

    def test_manual_accept_does_not_start_automatic_search(self):
        window, profile, source = self.open_window(mixture=True)
        window._set_search_mode("search")
        window.candidate_workspace.table.cellDoubleClicked.emit(0, 2)
        self.assertEqual([row.candidate.key for row in profile.selected_results], ["a"])
        self.assertFalse(window.residual_job_controller.is_running())
        self.assertEqual(source.search_count, 0)


if __name__ == "__main__":
    unittest.main()
