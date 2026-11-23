from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox, QPushButton

from vibrational_finder.io import PORTABLE_PROJECT_SUFFIX, load_project, save_project
from vibrational_finder.metadata import APP_NAME
from vibrational_finder.project_document import snapshot_project
from vibrational_finder.project_gui_bridge import project_open_filter, project_save_filter, project_save_path
from vibrational_finder.project_state import VibrationalProjectState
from vibrational_finder.raman_product import require_raman_project
from vibrational_finder.ui.background_task import BackgroundTaskHandle
from vibrational_finder.ui.workspace_preferences import (
    LAST_IMPORT_DIRECTORY_KEY,
    LAST_PROJECT_DIRECTORY_KEY,
    LAST_PROJECT_SAVE_DIRECTORY_KEY,
)


class ProjectDocumentActionsMixin:
    """Project actions; only complete snapshots cross the worker boundary."""

    def _init_project_file_actions(self) -> None:
        self._project_file_job: BackgroundTaskHandle | None = None
        self._project_file_jobs: list[BackgroundTaskHandle] = []
        self._project_file_operation: str | None = None
        self._project_open_done = None
        self._confirmed_project_close = False
        self._project_open_cancel = QPushButton("Cancel opening", self)
        self.statusBar().addPermanentWidget(self._project_open_cancel)
        self._project_open_cancel.hide()
        self._project_open_cancel.clicked.connect(self._cancel_project_open)

    def new_project(self) -> None:
        from vibrational_finder.ui.plot_display_actions import saved_finder_plot_settings

        project = VibrationalProjectState()
        project.ui.plot_view_settings.update(saved_finder_plot_settings())
        self.apply_project_state(project, path=None, take_ownership=True)

    def request_new_project(self) -> bool:
        if self._project_file_busy():
            return False
        return self._request_document_replacement("create a new project", self.new_project)

    def request_open_project(self) -> bool:
        if self._project_file_busy():
            return False
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Open Raman project",
            self.workspace_preferences.last_directory(
                LAST_PROJECT_DIRECTORY_KEY, LAST_PROJECT_SAVE_DIRECTORY_KEY, LAST_IMPORT_DIRECTORY_KEY,
            ),
            project_open_filter(),
        )
        return self.request_open_project_path(Path(path)) if path else False

    def request_open_project_path(self, path: Path, *, done=None) -> bool:
        if self._project_file_busy():
            return False
        return self._request_document_replacement(
            "open another project", lambda: self._start_project_open(Path(path), done), on_abort=done,
        )

    def _start_project_open(self, source: Path, done) -> None:
        self._cancel_analysis_jobs()
        current, revision = self.document.state, self.document.revision
        self._project_open_done = done

        def apply(restored: VibrationalProjectState) -> None:
            if self.document.state is not current or self.document.revision != revision:
                self.statusBar().showMessage("Project changed while opening; the current project was kept.", 8000)
                return
            self._install_opened_project(restored, source)
            self.statusBar().showMessage(f"Project opened: {source}", 8000)

        self._start_project_file_job("Open", source, lambda: load_project(source), apply)

    def request_save_project(self, *, after_save=None, on_abort=None) -> bool:
        if self._project_file_busy():
            return False
        target = self.document.path
        if target is None:
            directory = self.workspace_preferences.last_directory(
                LAST_PROJECT_SAVE_DIRECTORY_KEY, LAST_PROJECT_DIRECTORY_KEY, LAST_IMPORT_DIRECTORY_KEY,
            )
            default = Path(directory) / f"raman_project{PORTABLE_PROJECT_SUFFIX}"
            path, _selected_filter = QFileDialog.getSaveFileName(
                self,
                "Save Raman project",
                str(default),
                project_save_filter(),
            )
            if not path:
                return False
            target = Path(project_save_path(path))
        current = self.collect_project_state()
        snapshot = snapshot_project(current)
        revision = self.document.revision

        def apply(saved: Path) -> None:
            if self.document.state is not current:
                if on_abort is not None:
                    on_abort()
                return
            self.document.mark_saved(saved, revision=revision)
            self.workspace_preferences.remember_directory(LAST_PROJECT_SAVE_DIRECTORY_KEY, saved.parent)
            if self.document.dirty:
                self.statusBar().showMessage("Project saved; newer changes are still unsaved.", 8000)
                if on_abort is not None:
                    on_abort()
                return
            self.statusBar().showMessage(f"Project saved: {saved}", 8000)
            if after_save is not None:
                after_save()

        self._start_project_file_job("Save", target, lambda: save_project(snapshot, target), apply, on_abort)
        return True

    def _project_file_busy(self) -> bool:
        if self._project_file_job is None:
            return False
        self.statusBar().showMessage("Wait for the current project operation to finish.", 4000)
        return True

    def _start_project_file_job(self, operation, path, task, apply, on_failure=None) -> None:
        handle = BackgroundTaskHandle(task, self)
        self._project_file_job = handle
        self._project_file_jobs.append(handle)
        self._project_file_operation = operation
        self._project_open_cancel.setVisible(operation == "Open")
        self.statusBar().showMessage(f"{'Opening' if operation == 'Open' else 'Saving'} project: {path.name}")

        def finish(result=None, error=None) -> None:
            if self._project_file_job is not handle:
                return
            done = self._reset_project_file_job()
            try:
                if error is None:
                    apply(result)
                else:
                    self.statusBar().showMessage(f"{operation} project failed", 8000)
                    QMessageBox.warning(self, f"{operation} project", f"Could not {operation.lower()} project:\n{path}\n\n{error}")
                    if on_failure is not None:
                        on_failure()
            finally:
                if done is not None:
                    done()

        handle.finished.connect(finish)
        handle.failed.connect(lambda error, _details: finish(error=error))
        handle.drained.connect(lambda: self._project_file_jobs.remove(handle))
        self._background_lane.submit(handle)

    def _reset_project_file_job(self):
        self._project_file_job = None
        self._project_file_operation = None
        self._project_open_cancel.hide()
        done, self._project_open_done = self._project_open_done, None
        return done

    def _cancel_project_open(self) -> None:
        if self._project_file_operation != "Open":
            return
        handle = self._project_file_job
        done = self._reset_project_file_job()
        self._background_lane.cancel(handle)
        self.statusBar().showMessage("Opening cancelled; the current project was kept.", 8000)
        if done is not None:
            done()

    def open_project_path(self, path: Path) -> VibrationalProjectState:
        source = Path(path)
        restored = load_project(source)
        self._install_opened_project(restored, source)
        return self.project

    def _install_opened_project(self, restored: VibrationalProjectState, source: Path) -> None:
        self.apply_project_state(restored, path=source, take_ownership=True)
        self.workspace_preferences.remember_directory(LAST_PROJECT_DIRECTORY_KEY, source.parent)

    def save_project_path(self, path: Path | None = None) -> Path:
        target = Path(path) if path is not None else self.document.path
        if target is None:
            raise ValueError("A project path is required for the first save.")
        state = self.collect_project_state()
        saved = save_project(state, target)
        self.document.mark_saved(saved)
        self.workspace_preferences.remember_directory(LAST_PROJECT_SAVE_DIRECTORY_KEY, saved.parent)
        return saved

    def collect_project_state(self) -> VibrationalProjectState:
        state = self.project
        state.active_spectrum_key = self.active_spectrum().id if self.active_spectrum() is not None else None
        state.visible_observed_keys = set(self.project_tree.checked_spectrum_ids())
        state.ui.display_mode = self.plot_control_bar.spectrum_display_mode.currentText()
        state.ui.multi_offset = self.plot_control_bar.offset_slider.value()
        state.ui.normalization_mode = "max" if self.plot_control_bar.normalize_checkbox.isChecked() else "none"
        plot_settings = deepcopy(state.ui.plot_view_settings)
        plot_settings["aspect_mode"] = self.plot_control_bar.plot_aspect_mode.currentText()
        state.ui.plot_view_settings = plot_settings
        state.ui.tree_expansion_state = self.project_tree.expansion_state()
        state.ui.search_mode = "auto" if self._search_mode == "auto" else "search"
        state.normalize()
        self.document.state = state
        return state

    def apply_project_state(
        self, state: VibrationalProjectState, *, path: Path | None = None, take_ownership: bool = False,
    ) -> None:
        require_raman_project(state)
        self._cancel_project_open()
        restored = state if take_ownership else snapshot_project(state)
        self.document.replace_state(restored, path=path)
        self.project = self.document.state
        if hasattr(self, "_analysis_project_replaced"):
            self._analysis_project_replaced()

        ui = self.project.ui
        self._set_combo_text(self.plot_control_bar.spectrum_display_mode, ui.display_mode)
        self._set_combo_text(
            self.plot_control_bar.plot_aspect_mode,
            str(ui.plot_view_settings.get("aspect_mode", "Fit")),
        )
        self._set_slider_value(self.plot_control_bar.offset_slider, max(0, min(int(ui.multi_offset), 150)))
        self._sync_plot_mode_controls()
        blocked = self.plot_control_bar.normalize_checkbox.blockSignals(True)
        self.plot_control_bar.normalize_checkbox.setChecked(ui.normalization_mode != "none")
        self.plot_control_bar.normalize_checkbox.blockSignals(blocked)
        self._sync_all_black_button()

        blocked = self.project_tree.blockSignals(True)
        try:
            self._refresh_project_tree()
            self.project_tree.set_checked_spectrum_ids(list(self.project.visible_observed_keys))
            self.project_tree.restore_expansion_state(ui.tree_expansion_state)
        finally:
            self.project_tree.blockSignals(blocked)
        self._apply_plot_aspect()
        self.set_active_spectrum(self.project.active_spectrum_key, prepare_gain=False)
        self.setWindowTitle(f"{APP_NAME} — {self.project.project_name}")

    def _request_document_replacement(self, action: str, proceed, *, on_abort=None) -> bool:
        if not self.document.dirty:
            proceed()
            return True
        choice = QMessageBox.question(
            self,
            "Unsaved project",
            f"Save changes before you {action}?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Discard:
            proceed()
            return True
        if choice == QMessageBox.StandardButton.Save:
            return self.request_save_project(after_save=proceed, on_abort=on_abort)
        return False

    def _prepare_project_close(self) -> bool:
        if self._project_file_operation == "Save":
            self._project_file_busy()
            return False
        self._cancel_project_open()
        if self.isVisible() and self.document.dirty and not self._confirmed_project_close:
            def close_confirmed() -> None:
                self._confirmed_project_close = True
                QTimer.singleShot(0, self.close)
            self._request_document_replacement("close the project", close_confirmed)
            return False
        self._confirmed_project_close = False
        return all(handle.wait() for handle in list(self._project_file_jobs))

    def request_update_install(self, launch) -> bool:
        if self._project_file_operation == "Save":
            self._project_file_busy()
            return False

        def confirmed() -> None:
            if launch():
                self._confirmed_project_close = True
                self.close()

        return self._request_document_replacement("install the update", confirmed)

    @staticmethod
    def _set_combo_text(combo, value: str) -> None:
        index = combo.findText(value)
        if index < 0:
            return
        blocked = combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(blocked)

    @staticmethod
    def _set_slider_value(slider, value: int) -> None:
        blocked = slider.blockSignals(True)
        slider.setValue(value)
        slider.blockSignals(blocked)
