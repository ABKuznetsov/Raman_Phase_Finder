from __future__ import annotations

from collections import OrderedDict

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QMessageBox

from vibrational_finder.processing_controller import SpectrumProcessingController
from vibrational_finder.band_controller import BandController
from vibrational_finder.phase_subtraction_work import (
    PhaseFitController, PhaseFitKey, PhaseFitOutcome, PhaseFitRequest, PhaseFitResult, phase_fit_key,
)
from vibrational_finder.processing_state import ProcessingOperation
from vibrational_finder.project_model import observed_by_id
from vibrational_finder.services.phase_deconvolution import (
    PHASE_FIT_MODEL_VERSION, PHASE_SUBTRACTION_METADATA, PhaseSubtractionFit, phase_reference_bands, unpack_subtraction,
)
from vibrational_finder.services.preprocessing_service import auto_smoothing_window
from vibrational_finder.ui.analysis_job_controller import AnalysisJobController
from vibrational_finder.ui.phase_deconvolution_panel import PhaseDeconvolutionPanel
from vibrational_finder.ui.preprocessing_panels import (
    BackgroundRemovalPanel,
    DespikePanel,
    SmoothPanel,
    preprocessing_panel_style,
)
from vibrational_finder.ui.spectrum_transform_panels import (
    AxisCalibrationPanel,
    CropPanel,
    NormalizationPanel,
)
from vibrational_finder.ui.vibrational_plot import show_observed_spectrum
from vibrational_finder.ui.band_workspace import BandWorkspace


class SpectrumProcessingActionsMixin:
    """Connect inline spectroscopy editors to the transactional processing core."""

    def _init_spectrum_processing_actions(self) -> None:
        self.processing_controller = SpectrumProcessingController(self.document)
        self.band_controller = BandController(self.document)
        self._active_processing_key: str | None = None
        self._active_processing_panel = None
        self._processing_previous_range: list[list[float]] | None = None
        self._processing_preview_operation: ProcessingOperation | None = None
        self._phase_subtraction_preview: PhaseSubtractionFit | None = None
        self._phase_fit_request: PhaseFitRequest | None = None
        self._phase_fit_key: PhaseFitKey | None = None
        self._phase_fit_cache: OrderedDict[PhaseFitKey, PhaseFitResult] = OrderedDict()
        self._failed_phase_overlays: dict[str, PhaseFitKey] = {}
        self._phase_overlay_ids: tuple[str, ...] = ()
        self._phase_overlay_timer = QTimer(self, singleShot=True)
        self._phase_overlay_timer.timeout.connect(self._refresh_phase_overlays)
        self._stored_subtraction_cache = OrderedDict()
        # Overlay work must not keep automatic Gain or reference jobs waiting in their lane.
        self.phase_subtraction_job_controller = AnalysisJobController(
            PhaseFitController(self._apply_phase_fit), self,
        )
        self.phase_subtraction_job_controller.failed_for_request.connect(self._phase_fit_failed)
        self.accepted_components_workspace.measuredSubtractionRequested.connect(self._subtract_measured_phase)
        self.action_bar.smoothRequested.connect(self._open_smooth_panel)
        self.action_bar.subtractBackgroundRequested.connect(self._open_background_panel)
        self.action_bar.phaseDeconvolutionRequested.connect(self._open_phase_deconvolution_panel)
        self.action_bar.despikeRequested.connect(self._open_despike_panel)
        self.action_bar.cropRequested.connect(self._open_crop_panel)
        self.action_bar.normalizeRequested.connect(self._open_normalization_panel)
        self.action_bar.axisCalibrationRequested.connect(self._open_axis_calibration_panel)
        self.action_bar.detectBandsRequested.connect(self._open_band_workspace)
        self.action_bar.undoProcessingRequested.connect(self.undo_active_processing)
        self.action_bar.resetProcessingRequested.connect(self.reset_active_processing)
        self.action_bar.processingSectionCollapsed.connect(self._processing_section_collapsed)
        self.plot_control_bar.axisCalibrationChanged.connect(self._calibration_indicator_changed)
        self._syncing_transform_indicators = False
        self._refresh_processing_controls()

    def apply_project_state(self, state, *, path=None, take_ownership=False) -> None:
        self.cancel_processing_preview()
        self._clear_phase_deconvolution()
        self._phase_fit_cache.clear()
        self._failed_phase_overlays.clear()
        self._stored_subtraction_cache.clear()
        super().apply_project_state(state, path=path, take_ownership=take_ownership)

    def _subtract_measured_phase(self, key: str | None = None) -> None:
        spectrum = self.active_spectrum()
        if spectrum is None:
            return
        profile = self.project.profile_states.get(spectrum.id)
        selected = profile.selected_results if profile is not None else ()
        chosen = next((result.candidate for result in selected if result.candidate.key == key), None)
        if key is not None and chosen is None:
            return
        references = [
            trace for trace in self._ordered_spectra()
            if trace.id != spectrum.id and (trace.kind, trace.x_unit, trace.y_unit) ==
            (spectrum.kind, spectrum.x_unit, spectrum.y_unit)
        ]
        if not references:
            QMessageBox.information(
                self, "Subtract measured spectrum",
                "Import a second Raman spectrum with the same axis units, then try again.",
            )
            return
        from vibrational_finder.ui.measured_phase_subtraction_dialog import MeasuredPhaseSubtractionDialog

        bands = phase_reference_bands(spectrum, selected) if chosen is not None else {}
        other_bands = [band for owner, group in bands.items() if owner != key for band in group]
        phase_name = (chosen.name or chosen.entry_id) if chosen is not None else None
        dialog = MeasuredPhaseSubtractionDialog(
            spectrum, references, phase_name, bands.get(key, ()), other_bands, self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.operation is None:
            return
        try:
            residual = self.processing_controller.save_phase_residual(spectrum.id, dialog.operation)
        except ValueError as exc:
            QMessageBox.warning(self, "Subtract measured spectrum", str(exc))
            return
        self._refresh_project_tree()
        self.set_active_spectrum(residual.id)
        self.statusBar().showMessage(f"Saved: {residual.name}", 6000)

    def _open_smooth_panel(self) -> None:
        spectrum = self.active_spectrum()
        if spectrum is None:
            self._refresh_processing_controls()
            return
        panel = SmoothPanel(auto_smoothing_window(np.asarray(spectrum.x), np.asarray(spectrum.y)))
        self._mount_processing_panel("smooth", panel)

    def _open_background_panel(self) -> None:
        if self.active_spectrum() is None:
            self._refresh_processing_controls()
            return
        self._mount_processing_panel("background", BackgroundRemovalPanel())

    def _open_phase_deconvolution_panel(self) -> None:
        if self.active_spectrum() is None:
            return
        if self._active_processing_key is not None:
            self.cancel_processing_preview()
        self._processing_previous_range = self.spectrum_plot.viewRange()
        settings = self.project.ui.plot_view_settings
        panel = PhaseDeconvolutionPanel(
            {}, settings.get("subtraction_phase_layers", {}),
            background_visible=settings.get("subtraction_background_visible", True),
            experiment_visible=settings.get("subtraction_experiment_visible", False),
            residual_visible=settings.get("subtraction_residual_visible", True),
        )
        self.action_bar.set_section_editor("phase_deconvolution", panel)
        self._active_processing_key = "phase_deconvolution"
        self._active_processing_panel = panel
        panel.saveRequested.connect(self._save_phase_output)
        panel.cancelRequested.connect(self.cancel_processing_preview)
        panel.chooseSpectrumRequested.connect(self._subtract_measured_phase)
        panel.layerToggled.connect(lambda key, field, visible: self._set_subtraction_layers((key,), field, visible))
        panel.backgroundToggled.connect(lambda visible: self._set_plot_layer_visible("subtraction_background_visible", visible))
        panel.displayChanged.connect(self._store_phase_display_settings)
        self._fit_phase_deconvolution()

    def _store_phase_display_settings(self) -> None:
        panel = self._active_processing_panel
        if isinstance(panel, PhaseDeconvolutionPanel):
            settings = self.project.ui.plot_view_settings
            settings["subtraction_experiment_visible"] = panel.experiment_visible.isChecked()
            settings["subtraction_residual_visible"] = panel.residual_visible.isChecked()
            self.document.mark_changed()
            self._redraw_active_spectrum(preserve_x_range=True)

    def _fit_phase_deconvolution(self) -> None:
        panel = self._active_processing_panel
        spectrum = self.active_spectrum()
        if not isinstance(panel, PhaseDeconvolutionPanel) or spectrum is None:
            return
        self._phase_overlay_timer.stop()
        self._phase_fit_key = None
        self._phase_subtraction_preview = None
        profile = self.project.profile_states.get(spectrum.id)
        if profile is None or not profile.selected_results:
            self._clear_phase_deconvolution()
            self._show_phase_fit_result(panel, PhaseFitResult({}, None))
            return
        key = phase_fit_key(spectrum, profile.processing.revision, profile.selected_results)
        cached = self._phase_fit_cache.get(key) or self._saved_phase_fit(profile)
        if cached is not None:
            if self._phase_fit_request is not None and self._phase_fit_request.key != key:
                self._cancel_phase_fit_request()
            if self._saved_phase_fit(profile) is None:
                profile.phase_fit = cached
                self.document.mark_changed()
            self._phase_fit_key = key
            self._show_phase_fit_result(panel, cached)
            return
        panel.set_phases({}, {})
        panel.update_fit("Calculating phase subtraction…")
        self._start_phase_fit(key, spectrum, profile)

    @staticmethod
    def _saved_phase_fit(profile) -> PhaseFitResult | None:
        result = profile.phase_fit
        if result is not None and result.fit is not None:
            if result.fit.model_version != PHASE_FIT_MODEL_VERSION or any(
                band.phase_key is not None and band.reference_position is None for band in result.fit.bands
            ):
                return None
        return result

    def _start_phase_fit(self, key, spectrum, profile, *, saved_result=None) -> None:
        if self._phase_fit_request is not None and self._phase_fit_request.key == key:
            return
        request = PhaseFitRequest.snapshot(key, spectrum, profile.selected_results, saved_result=saved_result)
        self._phase_fit_request = request
        self.phase_subtraction_job_controller.start(request)

    def _current_phase_fit_key(self, spectrum) -> PhaseFitKey | None:
        profile = self.project.profile_states.get(spectrum.id) if spectrum is not None else None
        if spectrum is None or profile is None or not profile.selected_results:
            return None
        return phase_fit_key(spectrum, profile.processing.revision, profile.selected_results)

    def _schedule_phase_overlays(self, spectrum_ids: tuple[str, ...]) -> None:
        if self._active_processing_key not in {None, "bands"}:
            return
        self._phase_overlay_ids = spectrum_ids
        self._phase_overlay_timer.start(0)

    def _refresh_phase_overlays(self) -> None:
        # The timer coalesces redraws and keeps snapshots out of plot rendering.
        if self._active_processing_key not in {None, "bands"}:
            return
        spectra = observed_by_id(self.project)
        for spectrum_id in self._phase_overlay_ids:
            spectrum = spectra.get(spectrum_id)
            key = self._current_phase_fit_key(spectrum)
            if key is None or key in self._phase_fit_cache or self._failed_phase_overlays.get(spectrum_id) == key:
                continue
            profile = self.project.profile_states[spectrum_id]
            if not profile.visible_selected_candidate_keys:
                continue
            saved_result = self._saved_phase_fit(profile)
            if saved_result is not None and saved_result.profiles is not None:
                continue
            self._start_phase_fit(key, spectrum, profile, saved_result=saved_result)
            return
        self._cancel_phase_fit_request()

    def _plotted_phase_fit(self, spectrum) -> PhaseFitResult | None:
        key = self._current_phase_fit_key(spectrum)
        if key is None:
            return None
        result = self._phase_fit_cache.get(key)
        if result is not None:
            self._phase_fit_cache.move_to_end(key)
            return result
        return self._saved_phase_fit(self.project.profile_states[key.spectrum_id])

    def _apply_phase_fit(self, outcome: PhaseFitOutcome) -> bool:
        panel = self._active_processing_panel
        request = outcome.request
        if request is not self._phase_fit_request:
            return False
        self._phase_fit_request = None
        panel_open = isinstance(panel, PhaseDeconvolutionPanel)
        spectrum_id = request.key.spectrum_id
        current = None
        if panel_open:
            current = self.active_spectrum()
        elif spectrum_id in self._phase_overlay_ids and self._active_processing_key in {None, "bands"}:
            current = observed_by_id(self.project).get(spectrum_id)
        if request.key != self._current_phase_fit_key(current):
            if panel_open:
                self._fit_phase_deconvolution()
            return False
        self._failed_phase_overlays.pop(spectrum_id, None)
        self._phase_fit_cache[request.key] = outcome.result
        self._phase_fit_cache.move_to_end(request.key)
        while len(self._phase_fit_cache) > max(4, len(self._phase_overlay_ids)):
            self._phase_fit_cache.popitem(last=False)
        if panel_open:
            self._phase_fit_key = request.key
            self.project.profile_states[spectrum_id].phase_fit = outcome.result
            self.document.mark_changed()
            self._show_phase_fit_result(panel, outcome.result)
        else:
            self._redraw_active_spectrum(preserve_view=True)
        return True

    def _phase_fit_failed(self, request: PhaseFitRequest, message: str, _details: str) -> None:
        panel = self._active_processing_panel
        if request is not self._phase_fit_request:
            return
        self._phase_fit_request = None
        self._failed_phase_overlays[request.key.spectrum_id] = request.key
        if isinstance(panel, PhaseDeconvolutionPanel):
            panel.update_fit(message)
        else:
            self._phase_overlay_timer.start(0)

    def _refresh_phase_subtraction_rows(self, references=None) -> None:
        panel = self._active_processing_panel
        spectrum = self.active_spectrum()
        if not isinstance(panel, PhaseDeconvolutionPanel) or spectrum is None:
            return
        if references is None:
            references = {key: bands for key, (_name, _color, bands) in panel.phases.items()}
        profile = self.project.profile_states.get(spectrum.id)
        selected = profile.selected_results if profile is not None else ()
        phases = {
            (candidate := result.candidate).key: (
                candidate.name or candidate.entry_id,
                self.project.candidate_colors.get(candidate.key, "#1a73e8"), references.get(candidate.key, ()),
            )
            for result in selected
        }
        panel.set_phases(
            phases, self.project.ui.plot_view_settings.get("subtraction_phase_layers", {}),
            fit=self._phase_subtraction_preview,
        )

    def _show_phase_fit_result(self, panel: PhaseDeconvolutionPanel, result: PhaseFitResult) -> None:
        fit = self._phase_subtraction_preview = result.fit
        self._refresh_phase_subtraction_rows(result.references)
        if fit is None:
            panel.update_fit("No usable reference bands in the selected phases." if panel.phases
                             else "No card-based phases selected.")
        elif fit.bands:
            removed = sum(band.removed and any(band.curve_y) for band in fit.bands)
            shared = sum(band.ambiguous for band in fit.bands)
            panel.update_fit(
                f"Resolved bands: {removed} subtracted · {len(fit.bands) - removed} retained ({shared} shared or ambiguous).",
                can_save=removed > 0,
            )
        else:
            panel.update_fit("No resolved bands were found in the measured spectrum.")
        self._redraw_active_spectrum(preserve_x_range=True)

    def _subtraction_phase_info(self):
        panel = self._active_processing_panel
        if isinstance(panel, PhaseDeconvolutionPanel):
            return {key: (name, color) for key, (name, color, _bands) in panel.phases.items()}
        return {}

    def _plotted_subtraction(self, spectrum):
        if spectrum is None:
            return {}, (), None
        fit = self._phase_subtraction_preview
        if fit is not None and spectrum.id == self.project.active_spectrum_key:
            return self._subtraction_phase_info(), fit.bands, (fit.x, fit.background)
        metadata = self.project.sample_metadata.get(spectrum.id, {})
        stored = metadata.get(PHASE_SUBTRACTION_METADATA)
        if not stored:
            return {}, (), None
        cache = self._stored_subtraction_cache
        key = (spectrum.id, stored)
        if key not in cache:
            cache[key] = unpack_subtraction(stored)
            if len(cache) > 8:
                cache.popitem(last=False)
        cache.move_to_end(key)
        return cache[key]

    def _save_phase_output(self) -> None:
        panel = self._active_processing_panel
        spectrum, fit = self.active_spectrum(), self._phase_subtraction_preview
        if not isinstance(panel, PhaseDeconvolutionPanel) or spectrum is None or fit is None:
            return
        if self._phase_fit_key != self._current_phase_fit_key(spectrum):
            self._fit_phase_deconvolution()
            self._redraw_active_spectrum(preserve_x_range=True)
            return
        try:
            saved = self.processing_controller.save_phase_subtraction(spectrum.id, fit, self._subtraction_phase_info())
        except ValueError as exc:
            panel.update_fit(str(exc))
            return
        self._clear_processing_editor()
        self._refresh_project_tree()
        self.set_active_spectrum(saved.id)
        self.statusBar().showMessage(f"Saved: {saved.name}", 6000)

    def _clear_phase_deconvolution(self) -> None:
        self._phase_overlay_timer.stop()
        self._phase_overlay_ids = ()
        self._cancel_phase_fit_request()
        self._phase_fit_key = None
        self._phase_subtraction_preview = None

    def _cancel_phase_fit_request(self) -> None:
        self._phase_fit_request = None
        self.phase_subtraction_job_controller.cancel()

    def fit_active_plot(self) -> None:
        if self._active_processing_key in {None, "bands", "phase_deconvolution"}:
            self._redraw_active_spectrum()
        else:
            self.spectrum_plot.enableAutoRange()

    def _open_despike_panel(self) -> None:
        if self.active_spectrum() is None:
            self._refresh_processing_controls()
            return
        self._mount_processing_panel("despike", DespikePanel())

    def _open_crop_panel(self) -> None:
        spectrum = self.active_spectrum()
        if spectrum is None:
            self._refresh_processing_controls()
            return
        self._mount_processing_panel("crop", CropPanel(list(spectrum.x), spectrum.x_unit))

    def _open_normalization_panel(self) -> None:
        if self.active_spectrum() is None:
            self._refresh_processing_controls()
            return
        self._mount_processing_panel("normalize", NormalizationPanel())

    def _open_axis_calibration_panel(self) -> None:
        spectrum = self.active_spectrum()
        if spectrum is None:
            self._refresh_processing_controls()
            return
        self._mount_processing_panel("calibration", AxisCalibrationPanel(spectrum.x_unit))

    def _open_band_workspace(self) -> None:
        spectrum = self.active_spectrum()
        if spectrum is None:
            self._refresh_processing_controls()
            return
        self.cancel_processing_preview()
        workspace = BandWorkspace(self.band_controller)
        workspace.set_spectrum(spectrum.id)
        workspace.bandSelected.connect(self.select_band)
        workspace.bandsChanged.connect(self._bands_changed)
        self.action_bar.set_section_editor("bands", workspace)
        self._active_processing_key = "bands"
        self._active_processing_panel = workspace

    def _refresh_band_workspace(self) -> None:
        if self._active_processing_key == "bands" and isinstance(self._active_processing_panel, BandWorkspace):
            spectrum = self.active_spectrum()
            self._active_processing_panel.set_spectrum(spectrum.id if spectrum is not None else None)

    def _bands_changed(self) -> None:
        if hasattr(self, "_redraw_active_spectrum"):
            self._redraw_active_spectrum(preserve_view=True)

    def _mount_processing_panel(self, key: str, panel) -> None:
        self.cancel_processing_preview()
        self._processing_previous_range = self.spectrum_plot.viewRange()
        panel.setStyleSheet(preprocessing_panel_style(self._dark_theme))
        self.action_bar.set_section_editor(key, panel)
        self._active_processing_key = key
        self._active_processing_panel = panel
        panel.previewRequested.connect(self.preview_active_processing)
        panel.applyRequested.connect(self.apply_active_processing)
        panel.cancelRequested.connect(self.cancel_processing_preview)
        self.preview_active_processing()

    def _operation_from_panel(self) -> ProcessingOperation | None:
        panel = self._active_processing_panel
        key = self._active_processing_key
        if panel is None or key is None:
            return None
        if key == "smooth":
            return ProcessingOperation(
                "smooth",
                {
                    "method": panel.method(),
                    "window": panel.window_size(),
                    "polyorder": panel.polyorder(),
                    "gaussian_sigma": panel.gaussian_sigma(),
                    "passes": panel.passes(),
                },
            )
        if key == "background":
            parameters = {
                "method": panel.method(),
                "degree": panel.degree(),
                "lam": panel.lambda_value(),
                "asymmetry": panel.asymmetry(),
                "half_window": panel.half_window(),
                "percentile": panel.floor_percentile(),
            }
            return ProcessingOperation("background", parameters)
        if key == "despike":
            return ProcessingOperation(
                "despike",
                {
                    "threshold": panel.threshold(),
                    "max_width": panel.max_width(),
                    "median_window": panel.median_window(),
                    "passes": panel.passes(),
                },
            )
        if key == "crop":
            return ProcessingOperation(
                "crop",
                {"minimum": panel.minimum(), "maximum": panel.maximum()},
            )
        if key == "normalize":
            return ProcessingOperation("normalize", {"mode": panel.mode()})
        if key == "calibration":
            return ProcessingOperation("axis_offset", {"offset": panel.offset()})
        return None

    def preview_active_processing(self) -> None:
        spectrum = self.active_spectrum()
        operation = self._operation_from_panel()
        if spectrum is None or operation is None:
            return
        try:
            preview = self.processing_controller.preview(spectrum.id, operation)
        except ValueError as exc:
            self._processing_preview_operation = None
            if hasattr(self._active_processing_panel, "set_error"):
                self._active_processing_panel.set_error(str(exc))
            show_observed_spectrum(self.spectrum_plot, spectrum, preserve_x_range=True)
            self._apply_plot_appearance(spectrum=spectrum)
            return
        if hasattr(self._active_processing_panel, "set_error"):
            self._active_processing_panel.set_error("")
        background_y = None
        if operation.kind == "background" and len(preview.y) == len(spectrum.y):
            background_y = (np.asarray(spectrum.y) - np.asarray(preview.y)).tolist()
        show_observed_spectrum(
            self.spectrum_plot,
            preview,
            preview=True,
            background_y=background_y,
            preserve_x_range=True,
        )
        self._apply_plot_appearance(spectrum=preview)
        self._processing_preview_operation = operation

    def apply_active_processing(self) -> None:
        spectrum = self.active_spectrum()
        operation = self._operation_from_panel()
        if spectrum is None or operation is None:
            return
        spectrum_id = spectrum.id
        preview_range = (
            self.spectrum_plot.viewRange() if operation == self._processing_preview_operation else None
        )
        try:
            self.processing_controller.commit(spectrum_id, operation)
        except ValueError as exc:
            if hasattr(self._active_processing_panel, "set_error"):
                self._active_processing_panel.set_error(str(exc))
            return
        self._clear_processing_editor()
        self.set_active_spectrum(spectrum_id)
        self._restore_processing_view(preview_range)

    def _processing_section_collapsed(self, key: str) -> None:
        if key == self._active_processing_key and key != "bands":
            self.cancel_processing_preview()

    def cancel_processing_preview(self) -> None:
        had_editor = self._active_processing_key is not None
        previous_range = self._processing_previous_range
        self._clear_processing_editor()
        if had_editor:
            if hasattr(self, "_redraw_active_spectrum"):
                self._redraw_active_spectrum()
            else:
                show_observed_spectrum(self.spectrum_plot, self.active_spectrum())
            self._restore_processing_view(previous_range)
        self._refresh_processing_controls()

    def _restore_processing_view(self, view_range: list[list[float]] | None) -> None:
        if view_range is not None:
            self.spectrum_plot.setRange(xRange=view_range[0], yRange=view_range[1], padding=0)

    def _clear_processing_editor(self) -> None:
        key = self._active_processing_key
        self._clear_phase_deconvolution()
        self._active_processing_key = None
        self._active_processing_panel = None
        self._processing_previous_range = None
        self._processing_preview_operation = None
        if key is not None:
            self.action_bar.clear_section_editor(key, collapse=key == "phase_deconvolution")

    def undo_active_processing(self) -> None:
        spectrum = self.active_spectrum()
        if spectrum is None:
            return
        self.cancel_processing_preview()
        self.processing_controller.undo(spectrum.id)
        self.set_active_spectrum(spectrum.id)

    def reset_active_processing(self) -> None:
        spectrum = self.active_spectrum()
        if spectrum is None:
            return
        self.cancel_processing_preview()
        self.processing_controller.reset(spectrum.id)
        self.set_active_spectrum(spectrum.id)

    def _refresh_processing_controls(self) -> None:
        spectrum = self.active_spectrum()
        available = spectrum is not None
        self.action_bar.set_processing_available(
            available,
            "Import or select a Raman spectrum to enable processing.",
        )
        can_undo = available and self.processing_controller.can_undo(spectrum.id)
        self.action_bar.set_history_available(can_undo, can_undo)
        self.plot_control_bar.normalize_checkbox.setEnabled(available)
        self.plot_control_bar.axis_calibration_checkbox.setEnabled(available)
        self._sync_transform_indicators()

    def _processing_kinds(self) -> list[str]:
        spectrum = self.active_spectrum()
        if spectrum is None:
            return []
        profile = self.project.profile_states.get(spectrum.id)
        if profile is None:
            return []
        return [operation.kind for operation in profile.processing.operations]

    def _sync_transform_indicators(self) -> None:
        kinds = self._processing_kinds()
        self._syncing_transform_indicators = True
        try:
            self.plot_control_bar.axis_calibration_checkbox.setChecked(
                "axis_offset" in kinds or self._active_processing_key == "calibration"
            )
        finally:
            self._syncing_transform_indicators = False

    def _open_section_from_indicator(self, key: str) -> None:
        section = self.action_bar._preprocessing_sections[key]
        if not section.toggle.isChecked():
            section.toggle.click()
        else:
            self.action_bar._section_toggled(key, True)

    def _calibration_indicator_changed(self, checked: bool) -> None:
        if self._syncing_transform_indicators:
            return
        if checked:
            self._open_section_from_indicator("calibration")
            self._sync_transform_indicators()
            return
        self._undo_last_transform_kind("axis_offset")

    def _undo_last_transform_kind(self, kind: str) -> None:
        spectrum = self.active_spectrum()
        if spectrum is None:
            return
        kinds = self._processing_kinds()
        if kinds and kinds[-1] == kind:
            self.cancel_processing_preview()
            self.processing_controller.undo(spectrum.id)
            self.set_active_spectrum(spectrum.id)
        else:
            self._sync_transform_indicators()
