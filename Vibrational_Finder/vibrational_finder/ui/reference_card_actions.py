"""Manual-card UI: one replaceable preview, and a queue of explicit accepts."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from vibrational_finder.core.models import SignalKind
from vibrational_finder.models import VibrationalMatchResult
from vibrational_finder.reference_card_work import ReferenceCardController, ReferenceCardOutcome, ReferenceCardRequest
from vibrational_finder.reference_export_work import (
    ReferenceExportController, ReferenceExportOutcome, ReferenceExportRequest, reference_file_stem,
)
from vibrational_finder.services.spectroscopy_source_catalog import ProjectReferenceSource
from vibrational_finder.ui.analysis_job_controller import AnalysisJobController
from vibrational_finder.ui.workspace_preferences import LAST_EXPORT_DIRECTORY_KEY


class ReferenceCardActionsMixin:
    def _init_reference_card_actions(self) -> None:
        self._preview_result: VibrationalMatchResult | None = None
        self._card_cache: OrderedDict[tuple[int, str], VibrationalMatchResult] = OrderedDict()
        self._pending_card_preview: ReferenceCardRequest | None = None
        self._pending_card_accept: ReferenceCardRequest | None = None
        self._card_accept_queue: deque[ReferenceCardRequest] = deque()
        self._card_preview_timer = QTimer(self)
        self._card_preview_timer.setSingleShot(True)
        self._card_preview_timer.setInterval(85)
        self._card_preview_timer.timeout.connect(self._submit_card_preview)
        self.card_preview_jobs = AnalysisJobController(
            ReferenceCardController(self._apply_card_preview), self, lane=self._background_lane,
        )
        self.card_accept_jobs = AnalysisJobController(
            ReferenceCardController(self._apply_card_accept), self, lane=self._background_lane,
        )
        self.card_export_jobs = AnalysisJobController(
            ReferenceExportController(self._apply_reference_export), self, lane=self._background_lane,
        )
        self.card_export_jobs.failed.connect(
            lambda message, _details: QMessageBox.warning(self, "Reference export", message)
        )
        self.card_preview_jobs.failed_for_request.connect(self._card_preview_failed)
        self.card_accept_jobs.failed_for_request.connect(self._card_accept_failed)
        self.card_accept_jobs.succeeded.connect(lambda _outcome: self._start_next_card_accept())
        self.card_accept_jobs.stale.connect(lambda _outcome: self._start_next_card_accept())

    def _export_candidate_references(self, keys: tuple[str, ...], suffix: str) -> None:
        items = tuple(
            item for key in dict.fromkeys(keys)
            if (item := self.candidate_workspace.result_for_key(key, align_for_gain=False)
                or self._make_card_request(key)) is not None
        )
        self._export_references(items, suffix)

    def _export_selected_references(self, keys: tuple[str, ...], suffix: str) -> None:
        spectrum = self.active_spectrum()
        results, _ = self.accepted_components_controller.selected(spectrum.id if spectrum else None)
        selected = set(keys)
        self._export_references(tuple(item for item in results if item.candidate.key in selected), suffix)

    def _export_references(
        self, items: tuple[VibrationalMatchResult | ReferenceCardRequest, ...], suffix: str,
    ) -> None:
        if not items or suffix not in {".rref", ".json"}:
            return
        if self.card_export_jobs.is_running():
            self.statusBar().showMessage("A reference export is already running", 4000)
            return
        directory = self.workspace_preferences.last_directory(LAST_EXPORT_DIRECTORY_KEY)
        batch = len(items) > 1
        format_name = suffix[1:].upper()
        if batch:
            name = QFileDialog.getExistingDirectory(self, f"Export {len(items)} {format_name} cards", directory)
            if not name:
                return
            target = Path(name)
        else:
            item = items[0]
            record = item.record if isinstance(item, ReferenceCardRequest) else item.candidate
            stem = reference_file_stem(record.name, record.source, record.entry_id)
            name, _ = QFileDialog.getSaveFileName(
                self, f"Export reference as {format_name}", str(Path(directory) / f"{stem}{suffix}"),
                f"{format_name} (*{suffix})",
            )
            if not name:
                return
            target = Path(name)
            if target.suffix.lower() != suffix:
                target = target.with_name(target.name + suffix)
                if target.exists() and QMessageBox.question(
                    self, "Replace reference card?", f"Replace {target.name}?",
                ) != QMessageBox.StandardButton.Yes:
                    return
        self.statusBar().showMessage(f"Exporting {len(items)} reference card(s)…")
        self.card_export_jobs.start(ReferenceExportRequest(items, target, suffix, batch))

    def _apply_reference_export(self, outcome: ReferenceExportOutcome) -> bool:
        count = len(outcome.saved)
        if count:
            self.workspace_preferences.remember_directory(LAST_EXPORT_DIRECTORY_KEY, outcome.saved[0].parent)
        self.statusBar().showMessage(f"Exported {count} reference card(s)", 5000)
        if outcome.errors:
            summary = "\n".join(outcome.errors[:8])
            if len(outcome.errors) > 8:
                summary += f"\n… and {len(outcome.errors) - 8} more"
            QMessageBox.warning(self, "Reference export", f"Exported {count} card(s).\n\n{summary}")
        return True

    def _reference_spectrum_token(self) -> tuple[str | None, int]:
        spectrum_id = self.project.active_spectrum_key
        profile = self.project.profile_states.get(spectrum_id) if spectrum_id is not None else None
        return spectrum_id, profile.processing.revision if profile is not None else 0

    def _reference_request_is_current(self, request: ReferenceCardRequest) -> bool:
        return (
            self._reference_spectrum_token() == request.spectrum_token
            and self.candidate_workspace.record_for_key(request.record.key) is request.record
            and self._browse_sources_by_key.get(request.record.key) is request.source
        )

    def _make_card_request(self, key: str) -> ReferenceCardRequest | None:
        record = self.candidate_workspace.record_for_key(key)
        source = self._browse_sources_by_key.get(key)
        if record is None or source is None:
            return None
        cache_key = (id(source), key)
        cached = self._card_cache.get(cache_key)
        if cached is not None:
            self._card_cache.move_to_end(cache_key)
        return ReferenceCardRequest(record, source, self._reference_spectrum_token(), cached)

    def _remember_card(self, outcome: ReferenceCardOutcome) -> None:
        request = outcome.request
        # Project references follow edits in the document itself.
        if isinstance(request.source, ProjectReferenceSource):
            return
        cache_key = (id(request.source), request.record.key)
        self._card_cache[cache_key] = outcome.preview
        self._card_cache.move_to_end(cache_key)
        while len(self._card_cache) > 16:
            self._card_cache.popitem(last=False)

    def preview_candidate(self, candidate_key: str) -> None:
        self._cancel_card_preview()
        result = self.candidate_workspace.result_for_key(candidate_key)
        if result is not None:
            self._present_card_preview(result)
            return
        request = self._make_card_request(candidate_key)
        if request is None:
            return
        if request.cached is not None:
            self._present_card_preview(request.cached)
            return
        self._clear_candidate_preview()
        self._pending_card_preview = request
        name = request.record.name or request.record.entry_id
        self.candidate_workspace.status_label.setText(f"Loading reference: {name}…")
        self._card_preview_timer.start()

    def _submit_card_preview(self) -> None:
        request = self._pending_card_preview
        if request is not None and self._reference_request_is_current(request):
            self.card_preview_jobs.start(request)
        else:
            self._pending_card_preview = None

    def _present_card_preview(self, result: VibrationalMatchResult) -> bool:
        spectrum = self.active_spectrum()
        if spectrum is not None and result.candidate.kind not in {SignalKind.UNKNOWN, spectrum.kind}:
            self._clear_candidate_preview()
            self.candidate_workspace.status_label.setText("Select a spectrum of the same method to overlay this reference")
            return False
        self._preview_result = result
        self._redraw_candidate_preview()
        self.candidate_workspace.status_label.setText(f"Preview: {result.candidate.name or result.candidate.entry_id}")
        return True

    def _apply_card_preview(self, outcome: ReferenceCardOutcome) -> bool:
        if outcome.request is not self._pending_card_preview:
            return False
        self._pending_card_preview = None
        if not self._reference_request_is_current(outcome.request):
            return False
        self._remember_card(outcome)
        return self._present_card_preview(outcome.preview)

    def _card_preview_failed(self, request: ReferenceCardRequest, message: str, _details: str) -> None:
        if request is self._pending_card_preview:
            self._pending_card_preview = None
            if self._reference_request_is_current(request):
                self.candidate_workspace.status_label.setText(f"Reference preview unavailable: {message}")

    def _cancel_card_preview(self) -> None:
        self._card_preview_timer.stop()
        self._pending_card_preview = None
        self.card_preview_jobs.cancel()

    def _clear_candidate_preview(self) -> None:
        self._cancel_card_preview()
        if self._preview_result is not None:
            self._preview_result = None
            self._redraw_candidate_preview()

    def _queue_card_accept(self, key: str) -> None:
        if self.active_spectrum() is None and any(
            item.candidate.key == key for item in self.project.working_references
        ):
            return
        if (self._pending_card_accept is not None and self._pending_card_accept.record.key == key
                or any(request.record.key == key for request in self._card_accept_queue)):
            return
        request = self._make_card_request(key)
        if request is not None:
            self._cancel_card_preview()
            self._card_accept_queue.append(request)
            self._start_next_card_accept()

    def _start_next_card_accept(self) -> None:
        if self._pending_card_accept is not None:
            return
        while self._card_accept_queue:
            request = self._card_accept_queue.popleft()
            spectrum = self.active_spectrum()
            if self._reference_request_is_current(request):
                if spectrum is not None:
                    request = replace(request, spectrum=replace(spectrum, x=list(spectrum.x), y=list(spectrum.y)))
                self._pending_card_accept = request
                name = request.record.name or request.record.entry_id
                self.candidate_workspace.status_label.setText(f"Preparing reference: {name}…")
                self.card_accept_jobs.start(request)
                break

    def _apply_card_accept(self, outcome: ReferenceCardOutcome) -> bool:
        if outcome.request is not self._pending_card_accept:
            return False
        self._pending_card_accept = None
        spectrum_id, _revision = outcome.request.spectrum_token
        if not self._reference_request_is_current(outcome.request):
            return False
        self._remember_card(outcome)
        if spectrum_id is None:
            self._commit_working_reference(outcome.preview)
            name = outcome.preview.candidate.name or outcome.preview.candidate.entry_id
            self.candidate_workspace.status_label.setText(f"Selected: {name}")
            return True
        if outcome.accepted is None:
            return False
        self._commit_accepted_candidate(spectrum_id, outcome.accepted)
        candidate = outcome.accepted.candidate
        self.candidate_workspace.status_label.setText(f"Accepted: {candidate.name or candidate.entry_id}")
        return True

    def _card_accept_failed(self, request: ReferenceCardRequest, message: str, _details: str) -> None:
        if request is self._pending_card_accept:
            self._pending_card_accept = None
            if self._reference_request_is_current(request):
                self.candidate_workspace.status_label.setText(f"Cannot accept the reference: {message}")
            self._start_next_card_accept()

    def _cancel_reference_jobs(self) -> None:
        self._cancel_card_preview()
        self._pending_card_accept = None
        self._card_accept_queue.clear()
        self.card_accept_jobs.cancel()
