from __future__ import annotations

from sqlite3 import Error as SQLiteError

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog

from vibrational_finder.core.data_sources import SourceQuery
from vibrational_finder.core.models import SignalKind

from vibrational_finder.analysis_controller import AnalysisController, AnalysisSearchOutcome
from vibrational_finder.gain_work import GainEvidenceCache, GainTableController, GainTableOutcome, GainTableRequest, snapshot_gain
from vibrational_finder.models import VibrationalMatchResult
from vibrational_finder.profile_state import SavedGain
from vibrational_finder.reference_browse_work import ReferenceBrowseController, ReferenceBrowseOutcome, ReferenceBrowseRequest
from vibrational_finder.raman_gain import GainWindow, RamanPeakPriority
from vibrational_finder.services.candidate_search_service import CandidateSearchOptions
from vibrational_finder.services.source_management import SourceUpdateResult
from vibrational_finder.ui.analysis_job_controller import AnalysisJobController
from vibrational_finder.ui.auto_search_setup import AutoSearchSetup, PEAK_PRIORITY_LABELS
from vibrational_finder.ui.reference_card_actions import ReferenceCardActionsMixin
from vibrational_finder.ui.source_management_jobs import SourceManagementJobController
from vibrational_finder.accepted_components_controller import AcceptedComponentsController, REFERENCE_COLORS
from vibrational_finder.residual_search import (
    ResidualSearchController,
    ResidualSearchOutcome,
    ResidualSearchUnavailable,
    build_residual_request,
)


class SpectroscopyAnalysisActionsMixin(ReferenceCardActionsMixin):
    def _init_analysis_actions(self) -> None:
        self._init_reference_card_actions()
        self.analysis_controller = AnalysisController(self.document)
        self.accepted_components_controller = AcceptedComponentsController(self.document)
        lane = self._background_lane
        self.analysis_job_controller = AnalysisJobController(self.analysis_controller, self, lane=lane)
        self.browse_job_controller = AnalysisJobController(
            ReferenceBrowseController(self._apply_reference_browse), self, lane=lane,
        )
        self._gain_cache = GainEvidenceCache()
        self._gain_table_request: GainTableRequest | None = None
        self.residual_search_controller = ResidualSearchController(self.analysis_controller, self._gain_cache)
        self.residual_job_controller = AnalysisJobController(self.residual_search_controller, self, lane=lane)
        self.gain_table_job_controller = AnalysisJobController(
            GainTableController(self._gain_cache, self._apply_gain_table), self, lane=lane,
        )
        self.source_management_job_controller = SourceManagementJobController(
            self.source_settings_panel.manager,
            self,
            lane=lane,
        )
        self.auto_search_setup = AutoSearchSetup(self.workspace_preferences.settings, self)
        self.top_toolbar.matchRequested.connect(self._identify_compounds)
        self.top_toolbar.matchSettingsRequested.connect(self._show_auto_search_setup)
        self.top_toolbar.detectBandsRequested.connect(self._toggle_band_search)
        self.top_toolbar.residualSearchRequested.connect(self.start_residual_search)
        self.top_toolbar.databaseRequested.connect(self._show_source_settings)
        self.top_toolbar.addReferenceRequested.connect(self.source_settings_panel.add_reference)
        self.candidate_workspace.matchRequested.connect(self.start_match)
        self.candidate_workspace.cancelRequested.connect(self._cancel_analysis_jobs)
        self.composition_panel.query_edit.textChanged.connect(self._cancel_reference_browse)
        self.candidate_workspace.previewRequested.connect(self.preview_candidate)
        self.candidate_workspace.acceptRequested.connect(self.accept_candidate)
        self.candidate_workspace.exportRequested.connect(self._export_candidate_references)
        self.source_settings_panel.sourcesChanged.connect(self._analysis_sources_changed)
        self.source_settings_panel.updateRequested.connect(
            lambda name, package: self._start_source_operation(name, "update", package or None)
        )
        self.source_settings_panel.importArchiveRequested.connect(
            lambda name, package, path: self._start_source_operation(name, "import_archive", package, local_archive=path)
        )
        self.source_settings_panel.clearCacheRequested.connect(
            lambda name, package: self._start_source_operation(name, "clear", package or None)
        )
        self.source_settings_panel.buildIndexRequested.connect(
            lambda name: self._start_source_operation(name, "build_index")
        )
        self.source_settings_panel.clearIndexRequested.connect(
            lambda name: self._start_source_operation(name, "clear_index")
        )
        self.source_management_job_controller.pending.connect(self.source_settings_panel.set_operation_pending)
        self.source_management_job_controller.progress.connect(self.source_settings_panel.set_operation_progress)
        self.source_settings_panel.cancelOperationRequested.connect(self.source_management_job_controller.cancel)
        self.source_management_job_controller.succeeded.connect(self._source_operation_succeeded)
        self.source_management_job_controller.downloadBlocked.connect(self.source_settings_panel.offer_rruff_archive)
        self.source_management_job_controller.failed.connect(
            lambda name, operation, message, _details: self.source_settings_panel.set_operation_failed(
                name, operation, message
            )
        )
        self.source_management_job_controller.cancelled.connect(self.source_settings_panel.set_operation_cancelled)
        self.accepted_components_workspace.visibilityChanged.connect(self._set_component_visible)
        self.accepted_components_workspace.colorRequested.connect(self._change_component_color)
        self.accepted_components_workspace.removeRequested.connect(self._remove_component)
        self.accepted_components_workspace.clearRequested.connect(self._clear_components)
        self.accepted_components_workspace.exportRequested.connect(self._export_selected_references)
        self.analysis_job_controller.pending.connect(lambda _request: self.candidate_workspace.set_pending())
        self.analysis_job_controller.statusChanged.connect(self.candidate_workspace.set_search_progress)
        self.analysis_job_controller.succeeded.connect(self._ordinary_search_succeeded)
        self.analysis_job_controller.failed_for_request.connect(self._analysis_failed)
        self.analysis_job_controller.cancelled.connect(self.candidate_workspace.set_cancelled)
        self.analysis_job_controller.stale.connect(
            lambda outcome: self._analysis_failed(outcome.request, "Results became stale after the project changed")
        )
        self.browse_job_controller.pending.connect(lambda _request: self.candidate_workspace.set_browse_pending())
        self.browse_job_controller.cancelled.connect(lambda: self.candidate_workspace.finish_browse("Database search cancelled"))
        self.browse_job_controller.failed.connect(
            lambda message, _details: self.candidate_workspace.finish_browse(f"Database search failed: {message}")
        )
        self.residual_job_controller.pending.connect(lambda _request: self.candidate_workspace.set_pending(gain=True))
        self.residual_job_controller.statusChanged.connect(self.candidate_workspace.set_search_progress)
        self.residual_job_controller.succeeded.connect(self._residual_search_succeeded)
        self.residual_job_controller.failed_for_request.connect(self._analysis_failed)
        self.residual_job_controller.cancelled.connect(self.candidate_workspace.set_cancelled)
        self.residual_job_controller.stale.connect(
            lambda outcome: self._analysis_failed(outcome.request, "Next-component results became stale")
        )
        self.gain_table_job_controller.failed_for_request.connect(self._gain_table_failed)
        self._browse_sources_by_key: dict[str, object] = {}
        self._refresh_candidate_workspace()
        self._refresh_accepted_components_workspace()

    def _set_search_mode(self, mode: str, *, redraw: bool = True) -> None:
        if mode not in {"search", "auto", "bands"}:
            raise ValueError(f"Unknown search mode: {mode}")
        labels_changed = (self._search_mode == "bands") != (mode == "bands")
        self._search_mode = mode
        self.top_toolbar.set_search_mode(mode)
        self.composition_panel.set_search_mode(mode)
        if labels_changed and redraw:
            self._redraw_active_spectrum(preserve_view=True)

    def _identify_compounds(self) -> None:
        self.auto_search_setup.hide()
        self._set_search_mode("auto")
        self.start_match(auto_match=True)

    def _show_auto_search_setup(self) -> None:
        if self.auto_search_setup.isVisible():
            self.auto_search_setup.hide()
            return
        kind = SignalKind.RAMAN
        try:
            count = self.source_catalog.local_reference_card_count(kind)
        except (OSError, SQLiteError):
            count = None
        self.auto_search_setup.show_below(self.top_toolbar.match_button, count)

    def _toggle_band_search(self) -> None:
        if self._search_mode == "bands":
            self._cancel_analysis_jobs()
            self._set_search_mode("search")
            return
        if self.active_spectrum() is None:
            self.top_toolbar.set_search_mode(self._search_mode)
            self.candidate_workspace.set_unavailable("Import or select a Raman spectrum first")
            return
        self._cancel_analysis_jobs()
        self._set_search_mode("bands")
        self._open_band_workspace()
        self.right_tabs.setCurrentWidget(self.action_bar)

    def _search_candidates_in_current_mode(self, query_text: str = "") -> None:
        if self._search_mode == "search":
            self._browse_reference_sources(query_text)
            return
        if self._search_mode == "bands":
            spectrum = self.active_spectrum()
            bands = self.project.sample_bands.get(spectrum.id, []) if spectrum is not None else []
            if not bands:
                self.candidate_workspace.set_unavailable(
                    "Detect or add at least one Raman band before Band search"
                )
                return
        self.start_match(query_text, auto_match=self._search_mode in {"auto", "bands"})

    def _browse_reference_sources(self, query_text: str = "") -> None:
        self._cancel_analysis_jobs()
        self._clear_candidate_preview()
        kind = SignalKind.RAMAN
        sources = self.source_catalog.enabled_handles(kind)
        if not sources:
            self._browse_sources_by_key.clear()
            self.candidate_workspace.set_unavailable("No enabled reference source is available")
            return
        query = self._composition_query(query_text, kind)
        self.browse_job_controller.start(ReferenceBrowseRequest(sources, query))

    def _apply_reference_browse(self, outcome: ReferenceBrowseOutcome) -> bool:
        if self._search_mode != "search" or outcome.request.sources[0].catalog is not self.source_catalog:
            self.candidate_workspace.finish_browse("Database search cancelled")
            return False
        self._browse_sources_by_key = outcome.source_by_key
        self.candidate_workspace.finish_browse()
        self.candidate_workspace.set_records(outcome.records)
        if outcome.unavailable:
            count = len(outcome.records)
            noun = "reference" if count == 1 else "references"
            self.candidate_workspace.status_label.setText(
                f"{count} {noun}; unavailable: {', '.join(outcome.unavailable)}"
            )
        elif not outcome.records:
            self.candidate_workspace.status_label.setText("No references found; check the name and element filters")
        return True

    def _cancel_reference_browse(self, *_args) -> None:
        self.browse_job_controller.cancel()

    def start_match(self, query_text: str | None = None, *, auto_match: bool | None = None) -> None:
        self._cancel_analysis_jobs()
        self._clear_candidate_preview()
        spectrum = self.active_spectrum()
        if spectrum is None:
            self.candidate_workspace.set_unavailable("Select a Raman spectrum first")
            return
        text = self.candidate_workspace.query_edit.text().strip() if query_text is None else str(query_text).strip()
        profile = self.project.profile_states.get(spectrum.id)
        if self._search_mode == "auto" and profile is not None and profile.selected_results:
            self.start_residual_search(text, automatic=True)
            return
        sources = self.source_catalog.enabled_handles(spectrum.kind)
        if not sources:
            self.candidate_workspace.set_unavailable("No enabled source is available for this spectrum")
            return
        query = self._composition_query(text, spectrum.kind)
        staged_match = self._search_mode in {"auto", "bands"} if auto_match is None else auto_match
        request = self.analysis_controller.make_request(
            spectrum.id,
            sources,
            query,
            CandidateSearchOptions(
                auto_match=staged_match,
                primary_raman=staged_match and self._search_mode == "auto",
                candidate_gate=lambda candidate: query.allows_formula(candidate.formula),
                instrument_profile=self.instrument_profiles.active,
                peak_priority=self.auto_search_setup.peak_priority
                if staged_match and self._search_mode == "auto" and spectrum.kind == SignalKind.RAMAN
                else RamanPeakPriority.OFF,
            ),
            use_sample_bands=not staged_match or self._search_mode == "bands",
        )
        self.analysis_job_controller.start(request)

    def _composition_query(self, text: str, kind: SignalKind) -> SourceQuery:
        return SourceQuery(
            text=str(text).strip(),
            kind=kind,
            required_elements=frozenset(item for item, state in self.element_states.items() if state == "required"),
            excluded_elements=frozenset(item for item, state in self.element_states.items() if state == "excluded"),
        )

    def start_residual_search(self, query_text: str | None = None, *, automatic: bool | None = None) -> None:
        self._cancel_analysis_jobs()
        automatic = self._search_mode == "auto" if automatic is None else automatic
        spectrum = self.active_spectrum()
        if spectrum is None:
            self.candidate_workspace.set_unavailable("Select a Raman spectrum first")
            return
        profile = self.project.profile_states.get(spectrum.id)
        if automatic and profile is not None and profile.selected_results:
            self._set_candidate_gain_context(profile, refresh_scores=False)
            self.candidate_workspace.set_residual_results([], position_only=True)
        sources = self.source_catalog.enabled_handles(spectrum.kind)
        if not sources:
            self.candidate_workspace.set_unavailable("No enabled source is available for next-component search")
            return
        text = self.candidate_workspace.query_edit.text().strip() if query_text is None else query_text
        query = self._composition_query(text, spectrum.kind)
        gain = snapshot_gain(profile) if automatic else None
        if automatic and gain is None:
            self.candidate_workspace.set_failed("Stable Raman bands could not be evaluated for the accepted references")
            return
        options = CandidateSearchOptions(
            auto_match=automatic,
            candidate_gate=lambda candidate: query.allows_formula(candidate.formula),
            instrument_profile=self.instrument_profiles.active,
        )
        if gain is not None:
            options.peak_priority = self.auto_search_setup.peak_priority
        try:
            request = build_residual_request(
                self.analysis_controller,
                spectrum.id,
                sources,
                query,
                options,
                gain_input=gain,
            )
        except ResidualSearchUnavailable as exc:
            self.candidate_workspace.set_failed(str(exc))
            return
        self.residual_job_controller.start(request)

    def _residual_search_succeeded(self, outcome: ResidualSearchOutcome) -> None:
        spectrum = self.active_spectrum()
        request = outcome.request
        if spectrum is None or spectrum.id != request.search_request.token.spectrum_id:
            return
        self._cancel_gain_table()
        profile = self.project.profile_states.get(spectrum.id)
        options = request.search_request.options
        matching = options.matching_options
        self._set_candidate_gain_context(
            profile, request.evidence_bands, position_only=matching.position_only,
            gain_window=matching.gain_window, peak_priority=options.peak_priority,
            refresh_scores=False,
        )
        self.candidate_workspace.set_residual_results(
            profile.residual_results if profile is not None else [], position_only=matching.position_only,
        )
        if matching.position_only and not request.evidence_bands:
            self.candidate_workspace.status_label.setText("No unexplained bands remain")
            return
        result = outcome.result
        failures = getattr(result, "source_failures", {})
        scanned = getattr(result, "scanned_records", None)
        if scanned is not None or failures:
            message = f"Next component — {self.candidate_workspace.table.rowCount()} candidates"
            if scanned is not None:
                message += f" · {scanned:,} cards checked"
                if unreadable := result.unreadable_records:
                    message += f" · {unreadable:,} unreadable"
            if options.peak_priority is not RamanPeakPriority.OFF:
                message += f" · Priority: {PEAK_PRIORITY_LABELS[options.peak_priority]}"
            if failures:
                message += f" · unavailable: {', '.join(sorted(failures))}"
            self.candidate_workspace.status_label.setText(message)

    def _refresh_candidate_workspace(self, *, prepare_gain: bool = True) -> None:
        self._cancel_gain_table()
        spectrum = self.active_spectrum()
        profile = self.project.profile_states.get(spectrum.id) if spectrum is not None else None
        automatic = self._search_mode == "auto" and profile is not None and bool(profile.selected_results)
        accepted = {item.candidate.key for item in profile.selected_results} if automatic else set()
        saved = profile.saved_gain if automatic else None
        ranked = bool(automatic and (profile.residual_results or (saved and saved.ranked)))
        if ranked:
            bands = saved.bands if saved is not None else profile.residual_results[0].observed_bands
            window = saved.window if saved is not None else GainWindow.from_coordinates(spectrum.x)
            self._set_candidate_gain_context(
                profile, bands, position_only=True, gain_window=window,
                peak_priority=saved.priority if saved is not None else RamanPeakPriority.OFF,
                refresh_scores=False,
            )
            self.candidate_workspace.set_residual_results(
                [item for item in profile.residual_results if item.candidate.key not in accepted],
                position_only=True,
            )
            if not bands:
                self.candidate_workspace.status_label.setText("No unexplained bands remain")
            return
        if saved is not None:
            self._set_candidate_gain_context(
                profile, saved.bands, position_only=True, gain_window=saved.window,
                peak_priority=saved.priority, prepared_matches=saved.matches, refresh_scores=False,
            )
        else:
            self._set_candidate_gain_context(profile, refresh_scores=False)
        candidates = [item for item in profile.results if item.candidate.key not in accepted] if profile is not None else []
        self.candidate_workspace.set_results(candidates)
        work = snapshot_gain(profile) if automatic and saved is None and prepare_gain else None
        if work is None:
            return
        refs = tuple(
            (item.candidate.key, tuple(band.position for band in item.reference_bands))
            for item in candidates if item.reference_bands
        )
        request = GainTableRequest(work, refs)
        self._gain_table_request = request
        self.gain_table_job_controller.start(request)

    def _cancel_gain_table(self) -> None:
        self._gain_table_request = None
        self.gain_table_job_controller.cancel()

    def _apply_gain_table(self, outcome: GainTableOutcome) -> bool:
        if self._gain_table_request is not outcome.request or self._search_mode != "auto":
            return False
        spectrum = self.active_spectrum()
        if spectrum is None or spectrum.id != outcome.request.input.spectrum.id:
            return False
        self._gain_table_request = None
        profile = self.project.profile_states.get(spectrum.id)
        if profile is None or profile.processing.revision != outcome.request.input.key.processing_revision:
            return False
        evidence = outcome.evidence
        profile.saved_gain = SavedGain(
            evidence.bands, evidence.window, self.auto_search_setup.peak_priority, outcome.matches,
            available=evidence.available,
        )
        self.document.mark_changed()
        self._set_candidate_gain_context(
            profile, evidence.bands, position_only=True, gain_window=evidence.window,
            prepared_matches=outcome.matches,
        )
        if self._preview_result is not None and not profile.residual_results:
            self.preview_candidate(self._preview_result.candidate.key)
        return True

    def _gain_table_failed(self, request: GainTableRequest, message: str, _details: str) -> None:
        if request is self._gain_table_request:
            self._gain_table_request = None
            self.candidate_workspace.score_mode_label.setText("Gain unavailable")
            self.candidate_workspace.status_label.setText(message)

    def _analysis_failed(self, request, message: str, _details: str = "") -> None:
        token = getattr(request, "search_request", request).token
        if self.project.active_spectrum_key == token.spectrum_id:
            self.candidate_workspace.set_failed(message)

    def _ordinary_search_succeeded(self, outcome: AnalysisSearchOutcome) -> None:
        spectrum = self.active_spectrum()
        if spectrum is None or spectrum.id != outcome.request.token.spectrum_id:
            return
        self._refresh_candidate_workspace()
        result = getattr(outcome, "result", None)
        failures = getattr(result, "source_failures", {})
        count = self.candidate_workspace.table.rowCount()
        scanned = getattr(result, "scanned_records", None)
        if scanned is not None and (scanned or failures):
            message = f"{count} candidates · {scanned:,} cards checked"
            if unreadable := result.unreadable_records:
                message += f" · {unreadable:,} unreadable"
            if outcome.request.options.peak_priority is not RamanPeakPriority.OFF:
                message += f" · Priority: {PEAK_PRIORITY_LABELS[outcome.request.options.peak_priority]}"
            if failures:
                message += f" · unavailable: {', '.join(sorted(failures))}"
            self.candidate_workspace.status_label.setText(message)
            self.statusBar().showMessage(message, 5000)
        elif failures:
            names = ", ".join(sorted(failures))
            self.candidate_workspace.status_label.setText(
                f"{count} candidates; unavailable: {names}"
            )
        elif count == 0:
            self.candidate_workspace.status_label.setText("No candidates found; check the element filters and enabled sources")
        else:
            self.statusBar().showMessage(f"Search complete — {count} candidates", 5000)

    def accept_candidate(self, candidate_key: str) -> None:
        spectrum = self.active_spectrum()
        result = self.candidate_workspace.result_for_key(candidate_key)
        if result is None:
            self._queue_card_accept(candidate_key)
            return
        if spectrum is None:
            self._commit_working_reference(result)
        else:
            self._commit_accepted_candidate(spectrum.id, result)

    def _commit_working_reference(self, result: VibrationalMatchResult) -> None:
        self._cancel_card_preview()
        self._preview_result = None
        added = self.accepted_components_controller.add_working_reference(result)
        self._refresh_accepted_components_workspace()
        self.right_tabs.setCurrentWidget(self.composition_panel)
        self._redraw_active_spectrum(preserve_view=not added)
        self.statusBar().showMessage("Reference added to working set" if added else "Reference already selected", 3000)

    def _commit_accepted_candidate(self, spectrum_id: str, result: VibrationalMatchResult) -> None:
        self._cancel_card_preview()
        self._preview_result = None
        added = self.accepted_components_controller.add(spectrum_id, result)
        if added and self._search_mode != "auto":
            self._set_candidate_gain_context(self.project.profile_states.get(spectrum_id))
        if added:
            self._fit_phase_deconvolution()
        self._refresh_accepted_components_workspace()
        self.right_tabs.setCurrentWidget(self.composition_panel)
        self._redraw_active_spectrum(preserve_view=True)
        self.statusBar().showMessage("Component accepted" if added else "Component already accepted", 3000)
        if added and self._search_mode == "auto":
            self.start_residual_search(automatic=True)

    def _refresh_accepted_components_workspace(self) -> None:
        spectrum = self.active_spectrum()
        profile = self.project.profile_states.get(spectrum.id) if spectrum is not None else None
        colors = self.project.candidate_colors
        colored = {
            item.candidate.key: colors.get(item.candidate.key, REFERENCE_COLORS[0])
            for state in self.project.profile_states.values()
            for item in state.selected_results
        }
        if spectrum is None:
            colored = {item.candidate.key: colors.get(item.candidate.key, REFERENCE_COLORS[0])
                       for item in self.project.working_references}
        self.candidate_workspace.set_accepted_colors(colored)
        self._refresh_plot_settings_profile()
        if spectrum is None:
            self.accepted_components_workspace.set_components(
                self.project.working_references, self.project.visible_working_reference_keys, colors, scored=False,
            )
            return
        if profile is None:
            self.accepted_components_workspace.set_components([], set(), self.project.candidate_colors)
            return
        self.accepted_components_workspace.set_components(
            profile.selected_results,
            profile.visible_selected_candidate_keys,
            self.project.candidate_colors,
        )

    def _change_component_color(self, key: str) -> None:
        spectrum = self.active_spectrum()
        current = QColor(self.project.candidate_colors.get(key, REFERENCE_COLORS[0]))
        color = QColorDialog.getColor(current, self, "Select reference color")
        if color.isValid() and self.accepted_components_controller.set_color(
            spectrum.id if spectrum is not None else None, key, color.name(),
        ):
            self._refresh_phase_subtraction_rows()
            self._refresh_accepted_components_workspace()
            self._redraw_active_spectrum(preserve_view=True)

    def _set_candidate_gain_context(
        self, profile, evidence=None, *, position_only=False, gain_window=None,
        peak_priority=None, prepared_matches=None, refresh_scores=True,
    ) -> None:
        selected = profile.selected_results if profile is not None else ()
        gain_bands = list(evidence) if position_only and evidence is not None else None
        if evidence is None and selected:
            if self._search_mode != "auto" and any(result.observed_bands for result in selected):
                evidence = profile.cumulative_unassigned_bands
        self.candidate_workspace.set_gain_context(
            (band.position for band in evidence) if evidence is not None else None,
            (result.candidate.key for result in selected),
            gain_bands=gain_bands,
            gain_window=gain_window,
            peak_priority=(peak_priority if peak_priority is not None
                           else self.auto_search_setup.peak_priority if self._search_mode == "auto"
                           else RamanPeakPriority.OFF),
            prepared_matches=prepared_matches, refresh_scores=refresh_scores,
        )

    def _set_component_visible(self, key: str, visible: bool) -> None:
        spectrum = self.active_spectrum()
        if self.accepted_components_controller.set_visible(spectrum.id if spectrum else None, key, visible):
            self._refresh_accepted_components_workspace()
            self._redraw_active_spectrum(preserve_view=True)

    def _remove_component(self, key: str) -> None:
        spectrum = self.active_spectrum()
        if not self.accepted_components_controller.remove(spectrum.id if spectrum else None, key):
            return
        if spectrum is not None:
            self._refresh_after_component_removal()
        else:
            self._clear_candidate_preview()
            self._refresh_accepted_components_workspace()
            self._redraw_active_spectrum(preserve_view=True)

    def _clear_components(self) -> None:
        spectrum = self.active_spectrum()
        if not self.accepted_components_controller.clear(spectrum.id if spectrum else None):
            return
        if spectrum is not None:
            self._refresh_after_component_removal()
        else:
            self._cancel_reference_jobs()
            self._preview_result = None
            self._refresh_accepted_components_workspace()
            self._redraw_active_spectrum(preserve_view=True)

    def _refresh_after_component_removal(self) -> None:
        self._cancel_analysis_jobs()
        self._fit_phase_deconvolution()
        if self._search_mode == "auto":
            self._refresh_candidate_workspace()
        else:
            spectrum = self.active_spectrum()
            self._set_candidate_gain_context(self.project.profile_states.get(spectrum.id) if spectrum else None)
        self._refresh_accepted_components_workspace()
        self._redraw_active_spectrum(preserve_view=True)

    def _show_source_settings(self) -> None:
        self.source_settings_panel.ensure_populated()
        self.source_settings_dialog.show()
        self.source_settings_dialog.raise_()
        self.source_settings_dialog.activateWindow()

    def _analysis_sources_changed(self) -> None:
        self._cancel_analysis_jobs()
        self._card_cache.clear()
        self._clear_candidate_preview()

    def _start_source_operation(
        self, name: str, operation: str, package_key: str | None = None, *, local_archive: str | None = None,
    ) -> None:
        self._analysis_sources_changed()
        self.source_management_job_controller.start(name, operation, package_key, local_archive=local_archive)

    def _source_operation_succeeded(self, name: str, operation: str, result: object) -> None:
        if operation == "build_index" and isinstance(result, tuple) and len(result) == 2:
            message = f"{name}: indexed {result[0]}, skipped {result[1]}"
        elif operation in {"update", "import_archive"}:
            if isinstance(result, SourceUpdateResult) and result.index_counts is not None:
                indexed, skipped = result.index_counts
                message = f"{name}: ready locally ({indexed:,} indexed, {skipped:,} skipped)"
            else:
                message = f"{name}: cache updated"
        elif operation == "clear":
            message = f"{name}: local package deleted"
        else:
            message = f"{name}: line index cleared"
        self.source_settings_panel.set_operation_succeeded(name, operation, message)
        self.statusBar().showMessage(message, 5000)

    def _cancel_analysis_jobs(self) -> None:
        self.analysis_job_controller.cancel()
        self._cancel_reference_browse()
        self.residual_job_controller.cancel()
        self._cancel_gain_table()
        self._cancel_reference_jobs()

    def _analysis_project_replaced(self) -> None:
        self._preview_result = None
        self._card_cache.clear()
        self._browse_sources_by_key.clear()
        self._cancel_analysis_jobs()
        self.source_management_job_controller.cancel()
        self.source_catalog = self.source_catalog.for_project(self.project)
        self.source_settings_panel.set_catalog(self.source_catalog, refresh=False)
        self.source_management_job_controller.manager = self.source_settings_panel.manager
        self._restore_composition_filter_from_project()
        self.analysis_controller = AnalysisController(self.document)
        self.accepted_components_controller = AcceptedComponentsController(self.document)
        self.analysis_job_controller.analysis_controller = self.analysis_controller
        self._gain_cache = GainEvidenceCache()
        self.residual_search_controller = ResidualSearchController(self.analysis_controller, self._gain_cache)
        self.residual_job_controller.analysis_controller = self.residual_search_controller
        self.gain_table_job_controller.analysis_controller = GainTableController(self._gain_cache, self._apply_gain_table)
        mode = self.project.ui.search_mode
        active = self.project.profile_states.get(self.project.active_spectrum_key)
        if mode not in {"auto", "search"}:
            mode = "auto" if active is not None and (active.residual_results or active.saved_gain is not None) else "search"
        self._set_search_mode(mode, redraw=False)

    def _shutdown_analysis_jobs(self) -> None:
        if hasattr(self, "analysis_job_controller"):
            self._cancel_analysis_jobs()
            self.source_management_job_controller.cancel()
            self.analysis_job_controller.shutdown()
        if hasattr(self, "residual_job_controller"):
            self.residual_job_controller.shutdown()
        if hasattr(self, "browse_job_controller"):
            self.browse_job_controller.shutdown()
        if hasattr(self, "gain_table_job_controller"):
            self.gain_table_job_controller.shutdown()
        if hasattr(self, "source_management_job_controller"):
            self.source_management_job_controller.shutdown()
        if hasattr(self, "card_preview_jobs"):
            self.card_preview_jobs.shutdown()
            self.card_accept_jobs.shutdown()
            self.card_export_jobs.shutdown()
