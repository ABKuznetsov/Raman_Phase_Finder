from __future__ import annotations

import math
from dataclasses import asdict, replace

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QDialog, QInputDialog, QMenu, QVBoxLayout

from vibrational_finder.core.models import SignalKind
from vibrational_finder.metadata import DEFAULT_PLOT_TITLE
from vibrational_finder.plot_export.metadata import CanvasLayer, tag_canvas_item
from vibrational_finder.preprocessing import normalize_y
from vibrational_finder.ui.plot_canvas_appearance import apply_spectrum_appearance
from vibrational_finder.ui.plot_view_settings import PlotViewSettings, PlotViewSettingsWidget, finder_plot_defaults
from vibrational_finder.ui.spectrum_legends import PHASE_LEGEND_CHOICES, multi_legend_mode


ASPECT_RATIOS = {"1:1": 1.0, "2:1": 2.0, "4:2": 2.0, "Vertical 4:2": 0.5,
                 "18:6": 3.0, "4:3": 4 / 3, "16:9": 16 / 9}
LAYER_FIELDS = {
    "layer_reference_bottom_ticks_visible": "layer_reference_shift_ticks_visible",
    "layer_coverage_markers_visible": "layer_reference_peak_markers_visible",
}
SUBTRACTION_LAYER_OPTIONS = (
    ("Visible", "visible", True), ("Phase profile", "profile", False), ("Gaussians", "gaussians", False),
    ("Markers", "markers", True), ("Wavenumbers", "positions", False),
    ("Mode labels", "modes", False), ("Vibration assignments", "assignments", False),
)


def saved_finder_plot_settings() -> dict:
    settings = PlotViewSettingsWidget.saved_default_settings(finder_mode=True)
    if settings is None:
        return {}
    values = asdict(settings)
    ratio = values.pop("aspect_ratio")
    values["aspect_mode"] = "Fit" if ratio is None else next(
        (key for key, aspect in ASPECT_RATIOS.items() if aspect == ratio), "Custom",
    )
    return {LAYER_FIELDS.get(key, key): value for key, value in values.items()}


def _positive_number(value, default: float) -> float:
    try:
        value = float(value)
    except (ValueError, TypeError):
        return default
    return value if math.isfinite(value) and value > 0 else default


class PlotDisplayActionsMixin:
    def _init_plot_display_actions(self) -> None:
        self.top_toolbar.plotSettingsRequested.connect(self._show_plot_settings)
        controls = self.plot_control_bar
        for mode in ASPECT_RATIOS:
            if controls.plot_aspect_mode.findText(mode) < 0:
                controls.plot_aspect_mode.addItem(mode)
        self._plot_settings_dialog = None
        self._show_candidate_profile = False
        self._plot_cursor = None
        self._plot_axis_spectra = ()
        self.spectrum_plot.scene().sigMouseMoved.connect(self._move_plot_cursor)
        controls.normalize_checkbox.setChecked(self.project.ui.normalization_mode != "none")
        controls.normalizeSpectraChanged.connect(self._set_plot_normalization)
        self._sync_all_black_button()
        controls.allObservedBlackChanged.connect(
            lambda enabled: self._set_plot_layer_visible("all_observed_black", enabled)
        )
        controls.plotAspectModeChanged.connect(self._apply_plot_aspect)
        controls.spectrumDisplayModeChanged.connect(self._display_mode_changed)
        controls.spectrumOffsetPercentChanged.connect(self._display_offset_changed)
        self.project_tree.spectrum_selection_changed.connect(self._update_selected_plot)
        self.project_tree.spectrum_visibility_edited.connect(self._store_visible_spectra)
        self._plot_layer_menu = QMenu(self.spectrum_plot)
        self._phase_legend_menu = QMenu("Phase legend", self.spectrum_plot)
        self._phase_legend_menu.setObjectName("phaseLegendMenu")
        self._phase_legend_group = QActionGroup(self._phase_legend_menu)
        self._phase_legend_group.setExclusive(True)
        self._phase_legend_actions = {}
        for code, text in PHASE_LEGEND_CHOICES:
            action = self._phase_legend_menu.addAction(text)
            action.setData(code)
            action.setCheckable(True)
            self._phase_legend_group.addAction(action)
            self._phase_legend_actions[code] = action
            action.triggered.connect(lambda _checked, mode=code: self._set_multi_legend_mode(mode))
        self.spectrum_plot.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.spectrum_plot.customContextMenuRequested.connect(self._show_plot_layer_menu)
        self.spectrum_plot.secondaryClick.connect(self._show_plot_layer_menu)
        self._sync_plot_mode_controls()
        self._apply_plot_aspect()

    def _show_plot_layer_menu(self, point: QPoint) -> None:
        menu = self._plot_layer_menu
        if menu.isVisible():
            return
        # Detach the reusable submenu before clear(), which can delete attached actions.
        menu.removeAction(self._phase_legend_menu.menuAction())
        if old_submenu := getattr(self, "_subtraction_layer_menu", None):
            menu.removeAction(old_submenu.menuAction())
            old_submenu.deleteLater()
            self._subtraction_layer_menu = None
        menu.clear()
        menu.addAction("Export publication figure…", self.request_publication_export)
        menu.addAction("Plot settings…", self._show_plot_settings)
        menu.addAction("Show full spectrum", self.fit_active_plot)
        menu.addSeparator()
        settings = self.project.ui.plot_view_settings
        multi = self._in_multi_spectra_mode()
        legend = menu.addAction("Spectrum names" if multi else "Legend")
        legend.setCheckable(True)
        legend.setChecked(bool(settings.get("legend_visible", True)))
        legend.toggled.connect(lambda visible: self._set_plot_layer_visible("legend_visible", visible))
        if multi:
            self._phase_legend_actions[multi_legend_mode(settings)].setChecked(True)
            menu.addMenu(self._phase_legend_menu)
        menu.addSeparator()
        preview = self._preview_result
        reference = (preview.aligned_reference or preview.candidate.reference) if preview is not None else None
        candidate_profile = menu.addAction("Candidate profile")
        candidate_profile.setCheckable(True)
        candidate_profile.setEnabled(bool(reference and reference.x and reference.y)
                                     and settings.get("layer_preview_peak_positions_visible", True)
                                     and preview.candidate.key not in self._active_accepted_candidate_keys())
        candidate_profile.setChecked(self._show_candidate_profile)
        candidate_profile.setToolTip("Overlay the selected candidate's spectrum without changing its peak lines.")
        candidate_profile.toggled.connect(self._set_candidate_profile_visible)
        for label, key, default in (
            ("Grid", "grid_visible", False),
            ("Reference profile", "layer_phase_profiles_visible", True),
            ("Reference peak lines", "layer_phase_ticks_visible", True),
            ("Phase markers", "layer_reference_peak_markers_visible", False),
            ("Reference ticks below spectrum", "layer_reference_shift_ticks_visible", True),
            ("Mode labels", "layer_reference_mode_labels_visible", True),
            ("Vibration assignments", "layer_reference_assignment_labels_visible", False),
        ):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(bool(settings.get(key, default)))
            if key == "layer_reference_mode_labels_visible":
                action.setToolTip("Show the mode or symmetry above annotated Raman bands.")
            elif key == "layer_reference_assignment_labels_visible":
                action.setToolTip("Show the bond or vibration type above annotated Raman bands.")
            action.toggled.connect(lambda visible, field=key: self._set_plot_layer_visible(field, visible))
        markers_only = menu.addAction("Markers only")
        markers_only.setToolTip("Show round phase markers without lines inside peaks or labels. Keep lower reference ticks as set.")
        markers_only.triggered.connect(self._show_markers_only)
        self._add_subtraction_layer_menu(menu)
        menu.popup(self.spectrum_plot.viewport().mapToGlobal(point))

    def _active_accepted_candidate_keys(self) -> set[str]:
        spectrum = self.active_spectrum()
        profile = self.project.profile_states.get(spectrum.id) if spectrum is not None else None
        return {result.candidate.key for result in profile.selected_results} if profile is not None else set()

    def _set_candidate_profile_visible(self, visible: bool) -> None:
        self._show_candidate_profile = visible
        self._redraw_candidate_preview()

    def _add_subtraction_layer_menu(self, menu: QMenu) -> None:
        primary = self.active_spectrum()
        curves = (primary,) + tuple(item for item in self._plot_axis_spectra if primary is None or item.id != primary.id)
        phases = {}
        for spectrum in curves:
            phase_info, bands, _background = self._plotted_subtraction(spectrum)
            for key in dict.fromkeys(band.phase_key for band in bands):
                phases[key or ""] = phase_info.get(key, ("Unassigned / shared bands", "#515661"))[0]
        if not phases:
            return
        menu.addSeparator()
        layers = QMenu("Phase band layers", menu)
        self._subtraction_phase_menus = []
        self._subtraction_layer_menu = layers
        menu.addMenu(layers)
        layers.addAction("Show all Gaussians", lambda: self._set_subtraction_layers(phases, "gaussians", True))
        layers.addAction("Hide all Gaussians", lambda: self._set_subtraction_layers(phases, "gaussians", False))
        settings = self.project.ui.plot_view_settings
        for name, key in (("Removed band markers", "subtraction_removed_markers"),
                          ("Remaining band markers", "subtraction_remaining_markers"),
                          ("Background", "subtraction_background_visible")):
            action = layers.addAction(name)
            action.setCheckable(True)
            action.setChecked(bool(settings.get(key, True)))
            action.toggled.connect(lambda value, field=key: self._set_plot_layer_visible(field, value))
        layers.addSeparator()
        for key, name in phases.items():
            phase_menu = QMenu(name, layers)
            self._subtraction_phase_menus.append(phase_menu)
            layers.addMenu(phase_menu)
            stored = settings.get("subtraction_phase_layers", {}).get(key, {})
            for label, field, default in SUBTRACTION_LAYER_OPTIONS:
                if field == "profile" and not key:
                    continue
                action = phase_menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(bool(stored.get(field, default)))
                action.toggled.connect(
                    lambda value, target=key, prop=field: self._set_subtraction_layers((target,), prop, value)
                )

    def _set_subtraction_layers(self, phase_keys, field: str, visible: bool) -> None:
        settings = self.project.ui.plot_view_settings
        layers = dict(settings.get("subtraction_phase_layers", {}))
        for key in phase_keys:
            layers[key] = {**layers.get(key, {}), field: visible}
            if field in {"profile", "gaussians", "markers", "modes", "assignments", "positions"} and visible:
                layers[key]["visible"] = True
        settings["subtraction_phase_layers"] = layers
        panel = self._active_processing_panel
        if self._active_processing_key == "phase_deconvolution" and panel is not None:
            panel.sync_layers(layers)
        self.document.mark_changed()
        refit_y = visible and field in {"gaussians", "profile"}
        self._redraw_active_spectrum(preserve_view=not refit_y, preserve_x_range=refit_y)

    def _show_markers_only(self) -> None:
        self.project.ui.plot_view_settings.update(
            layer_reference_peak_markers_visible=True,
            layer_phase_ticks_visible=False,
            layer_reference_mode_labels_visible=False,
            layer_reference_assignment_labels_visible=False,
        )
        self.document.mark_changed()
        self._redraw_active_spectrum(preserve_view=True)
        self._sync_plot_settings_dialog()

    def _set_plot_layer_visible(self, key: str, visible: bool) -> None:
        self.project.ui.plot_view_settings[key] = visible
        if key == "subtraction_background_visible" and self._active_processing_key == "phase_deconvolution":
            box = self._active_processing_panel.background_visible
            blocked = box.blockSignals(True)
            box.setChecked(visible)
            box.blockSignals(blocked)
        self.document.mark_changed()
        self._redraw_active_spectrum(preserve_view=True)
        self._sync_plot_settings_dialog()

    def _sync_all_black_button(self) -> None:
        button = self.plot_control_bar.all_observed_black_button
        blocked = button.blockSignals(True)
        button.setChecked(bool(self.project.ui.plot_view_settings.get("all_observed_black", False)))
        button.blockSignals(blocked)

    def _in_multi_spectra_mode(self) -> bool:
        return self.plot_control_bar.spectrum_display_mode.currentText() == "All selected"

    def _set_multi_legend_mode(self, mode: str) -> None:
        settings = self.project.ui.plot_view_settings
        if mode != multi_legend_mode(settings):
            settings["multi_phase_legend_mode"] = mode
            self.document.mark_changed()
            self._redraw_active_spectrum(preserve_view=True)
            self._sync_plot_settings_dialog()

    def _remember_spectrum_legend_position(self, spectrum_id: str | None, x: float, y: float) -> None:
        settings = self.project.ui.plot_view_settings
        if spectrum_id is None:
            key = "reference_legend_position" if self.active_spectrum() is None else "multi_common_legend_position"
            settings[key] = [x, y]
            self.document.mark_changed()
            self._reposition_band_annotations()
            return
        settings_key = "multi_legend_positions" if self._in_multi_spectra_mode() and self._phase_subtraction_preview is None else "single_legend_positions"
        stored = settings.get(settings_key)
        positions = dict(stored) if isinstance(stored, dict) else {}
        if positions.get(spectrum_id) != [x, y]:
            positions[spectrum_id] = [x, y]
            settings[settings_key] = positions
            self.document.mark_changed()
            self._reposition_band_annotations()

    def _apply_plot_aspect(self) -> None:
        mode = self.plot_control_bar.plot_aspect_mode.currentText()
        settings = self.project.ui.plot_view_settings
        aspect = ASPECT_RATIOS.get(mode)
        if mode == "Custom":
            width = _positive_number(settings.get("custom_aspect_width"), 4.0)
            height = _positive_number(settings.get("custom_aspect_height"), 2.0)
            aspect = min(100.0, max(0.01, width / height))
        self.plot_canvas.set_aspect(aspect)
        self._sync_plot_settings_dialog()

    def _display_mode_changed(self) -> None:
        mode = self.plot_control_bar.spectrum_display_mode.currentText()
        if self.project.ui.display_mode != mode:
            self.project.ui.display_mode = mode
            self.document.mark_changed()
        self._sync_plot_mode_controls()
        if self._active_processing_key is not None and self._active_processing_key != "bands":
            self.cancel_processing_preview()
        self._redraw_active_spectrum()

    def _display_offset_changed(self, percent: int) -> None:
        if self.project.ui.multi_offset != percent:
            self.project.ui.multi_offset = percent
            self.document.mark_changed()
        self._update_selected_plot(preserve_view=True)

    def _store_visible_spectra(self, spectrum_ids: list[str]) -> None:
        visible = set(spectrum_ids)
        if self.project.visible_observed_keys != visible:
            self.project.visible_observed_keys = visible
            self.document.mark_changed()

    def _set_plot_normalization(self, enabled: bool) -> None:
        if self._active_processing_key not in {None, "bands", "phase_deconvolution"}:
            self.cancel_processing_preview()
        self.project.ui.normalization_mode = "max" if enabled else "none"
        self.document.mark_changed()
        self._redraw_active_spectrum(preserve_x_range=True)

    def _display_spectrum(self, spectrum):
        if spectrum is None or not self.plot_control_bar.normalize_checkbox.isChecked():
            return spectrum
        y = np.array(spectrum.y, dtype=float)
        finite = np.isfinite(y)
        if np.any(finite):
            y[finite] = normalize_y(y[finite], "max")
        return replace(spectrum, y=y.tolist())

    def _sync_plot_mode_controls(self) -> None:
        controls = self.plot_control_bar
        controls.offset_slider.setEnabled(self._in_multi_spectra_mode())
        controls.offset_value.setText(f"{controls.offset_slider.value()}%")

    def _update_selected_plot(self, *, preserve_view: bool = False) -> None:
        if (self._in_multi_spectra_mode()
                and self._active_processing_key in {None, "bands"}):
            self._redraw_active_spectrum(preserve_view=preserve_view)

    def _spectra_for_plot(self, active):
        if not self._in_multi_spectra_mode():
            return self._display_spectrum(active), ()
        checked = set(self.project_tree.checked_spectrum_ids())
        chosen = [item for item in self._ordered_spectra() if item.id in checked]
        if not chosen:
            return None, ()
        primary = active if active is not None and active.id in checked else chosen[0]
        # Axes, units and magnitudes must agree before spectra can share a canvas.
        axes = (primary.kind, primary.x_unit, primary.y_unit)
        chosen = [item for item in chosen if (item.kind, item.x_unit, item.y_unit) == axes]
        primary_index = next(index for index, item in enumerate(chosen) if item.id == primary.id)
        chosen = [self._display_spectrum(item) for item in chosen]
        primary = chosen[primary_index]
        finite_traces = [values[np.isfinite(values)] for values in (np.asarray(item.y) for item in chosen)]
        span = max((float(np.ptp(values)) for values in finite_traces if values.size), default=0.0)
        step = (span or 1.0) * self.plot_control_bar.offset_slider.value() / 100.0
        return primary, tuple((item, (index - primary_index) * step) for index, item in enumerate(chosen)
                              if item.id != primary.id)

    def _show_plot_settings(self) -> None:
        if self._plot_settings_dialog is None:
            dialog = QDialog(self)
            dialog.setObjectName("spectroscopyPlotSettings")
            dialog.setWindowTitle("Plot settings")
            dialog.resize(780, 680)
            panel = PlotViewSettingsWidget(dialog, finder_mode=True)
            layout = QVBoxLayout(dialog)
            layout.addWidget(panel)
            self._plot_settings_dialog = dialog
            self._plot_settings_panel = panel
            panel.settingsChanged.connect(self._store_live_plot_settings)
            phases = panel.profile_candidates_workspace
            phases.colorRequested.connect(self._change_component_color)
            phases.visibilityChanged.connect(self._set_component_visible)
            phases.removeRequested.connect(self._remove_component)
            phases.clearRequested.connect(self._clear_components)
            phases.exportRequested.connect(self._export_selected_references)
        self._sync_plot_settings_dialog(force=True)
        self._refresh_plot_settings_profile()
        self._plot_settings_dialog.show()
        self._plot_settings_dialog.raise_()
        self._plot_settings_dialog.activateWindow()

    def _plot_settings_model(self) -> PlotViewSettings:
        stored = self.project.ui.plot_view_settings
        defaults = asdict(finder_plot_defaults())
        for name, default in tuple(defaults.items()):
            value = stored.get(LAYER_FIELDS.get(name, name), default)
            if isinstance(default, bool):
                value = value if isinstance(value, bool) else default
            elif isinstance(default, (int, float)):
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    value = default
            elif isinstance(default, str) and not isinstance(value, str):
                value = default
            defaults[name] = value
        mode = self.plot_control_bar.plot_aspect_mode.currentText()
        defaults["aspect_ratio"] = ASPECT_RATIOS.get(mode)
        if mode == "Custom":
            defaults["aspect_ratio"] = (
                _positive_number(defaults["custom_aspect_width"], 4.0)
                / _positive_number(defaults["custom_aspect_height"], 2.0)
            )
        return PlotViewSettings(**defaults)

    def _sync_plot_settings_dialog(self, *, force: bool = False) -> None:
        dialog = getattr(self, "_plot_settings_dialog", None)
        if dialog is not None and (force or dialog.isVisible()):
            panel = self._plot_settings_panel
            panel.set_settings(self._effective_plot_settings())
            blocked = panel.blockSignals(True)
            panel.aspect_combo.setCurrentText(self.plot_control_bar.plot_aspect_mode.currentText())
            panel.blockSignals(blocked)

    def _store_live_plot_settings(self, value: PlotViewSettings) -> None:
        previous = asdict(self._effective_plot_settings())
        requests_nm = any(getattr(value, field) == "nm" and previous[field] != "nm"
                          for field in ("bottom_axis_scale", "top_axis_scale"))
        if requests_nm and self._wavelength_context() is None and not self._request_measured_laser():
            self._sync_plot_settings_dialog(force=True)
            return
        updated = {LAYER_FIELDS.get(key, key): val for key, val in asdict(value).items()
                   if val != previous[key] and key != "aspect_ratio"}
        bar = self.plot_control_bar.plot_aspect_mode
        mode = self._plot_settings_panel.aspect_combo.currentText()
        if mode != bar.currentText():
            blocked = bar.blockSignals(True)
            bar.setCurrentText(mode)
            bar.blockSignals(blocked)
            updated["aspect_mode"] = mode
        if not updated:
            return
        self.project.ui.plot_view_settings.update(updated)
        self.document.mark_changed()
        aspect = value.aspect_ratio
        self.plot_canvas.set_aspect(aspect)
        if self._processing_preview_operation is not None:
            self._apply_plot_appearance()
        else:
            self._redraw_active_spectrum(preserve_view=True)

    @staticmethod
    def _is_wavenumber_spectrum(spectrum) -> bool:
        return spectrum.x_unit.strip().lower().replace(" ", "") in {"cm-1", "cm⁻¹", "cm^-1", "1/cm"}

    def _measured_laser_nm(self, spectrum) -> float:
        metadata = self.project.sample_metadata.get(spectrum.id, {})
        value = str(metadata.get("laser_nm", "")).lower().strip().removesuffix("nm").strip().replace(",", ".")
        return _positive_number(value, 0.0)

    def _wavelength_context(self) -> tuple[SignalKind, float] | None:
        spectra = self._plot_axis_spectra
        if not spectra or not all(self._is_wavenumber_spectrum(spectrum) for spectrum in spectra):
            return None
        if spectra[0].kind == SignalKind.FTIR:
            return SignalKind.FTIR, 0.0
        if spectra[0].kind != SignalKind.RAMAN:
            return None
        lasers = [self._measured_laser_nm(spectrum) for spectrum in spectra]
        if all(laser > 0 and math.isclose(laser, lasers[0], rel_tol=0, abs_tol=0.01) for laser in lasers):
            return SignalKind.RAMAN, lasers[0]
        return None

    def _effective_plot_settings(self) -> PlotViewSettings:
        settings = self._plot_settings_model()
        if self._wavelength_context() is None:
            return replace(settings, bottom_axis_scale="wavenumber", top_axis_scale="wavenumber", x_display_unit="cm-1")
        return settings

    def _request_measured_laser(self) -> bool:
        spectra = self._plot_axis_spectra
        if len(spectra) != 1 or spectra[0].kind != SignalKind.RAMAN or not self._is_wavenumber_spectrum(spectra[0]):
            self.statusBar().showMessage(
                "Wavelength needs a spectrum in cm⁻¹. For stacked Raman spectra, set the same laser in Compound details.",
                8000,
            )
            return False
        spectrum = spectra[0]
        laser, accepted = QInputDialog.getDouble(
            self._plot_settings_dialog, "Raman excitation",
            f"{spectrum.name or 'Raman spectrum'} — measured laser wavelength (nm):", 0.0, 0.0, 100000.0, 3,
        )
        if not accepted or not math.isfinite(laser) or laser <= 0.0:
            if accepted:
                self.statusBar().showMessage("Enter a positive laser wavelength to use nm.", 5000)
            return False
        metadata = self.project.sample_metadata.setdefault(spectrum.id, {})
        metadata["laser_nm"] = f"{laser:g}"
        active = self.active_spectrum()
        if active is not None and active.id == spectrum.id:
            self.sample_metadata_panel.set_metadata(
                {**self.sample_metadata_panel.metadata(), **metadata}, spectrum_name=spectrum.name,
            )
        return True

    def _refresh_plot_settings_profile(self) -> None:
        panel = getattr(self, "_plot_settings_panel", None)
        if panel is None:
            return
        spectrum = self.active_spectrum()
        panel.set_active_profile_label(f"Active spectrum: {spectrum.name or spectrum.id}" if spectrum else
                                       "Reference comparison" if self.project.working_references else "")
        if spectrum is None:
            panel.profile_candidates_workspace.set_components(
                self.project.working_references, self.project.visible_working_reference_keys,
                self.project.candidate_colors, scored=False,
            )
            return
        profile = self.project.profile_states.get(spectrum.id) if spectrum else None
        panel.profile_candidates_workspace.set_components(
            profile.selected_results if profile else [],
            profile.visible_selected_candidate_keys if profile else set(),
            self.project.candidate_colors,
        )

    def _move_plot_cursor(self, position) -> None:
        view = self.spectrum_plot.getViewBox()
        spectrum = self.active_spectrum()
        if not (spectrum is not None or self.project.visible_working_reference_keys) or not (
            view.sceneBoundingRect().contains(position)
        ):
            if self._plot_cursor is not None:
                self._plot_cursor.hide()
            self.plot_control_bar.set_cursor_readout()
            return
        point = view.mapSceneToView(position)
        axis = self.spectrum_plot.getAxis("bottom")
        x = float(axis.display_values([point.x()])[0])
        self.plot_control_bar.set_cursor_readout(
            position=x if math.isfinite(x) else None, intensity=point.y(),
            kind=spectrum.kind if spectrum is not None else SignalKind.RAMAN,
            x_unit="nm" if axis.wavelength_context is not None else "cm⁻¹",
        )
        if self.project.ui.plot_view_settings.get("cursor_vertical_line_visible", False):
            if self._plot_cursor is None:
                self._plot_cursor = pg.InfiniteLine(angle=90, pen=pg.mkPen("#8f969e"))
                tag_canvas_item(self._plot_cursor, layer=CanvasLayer.CURSOR, object_id="plot-cursor", exportable=False)
                view.addItem(self._plot_cursor, ignoreBounds=True)
            self._plot_cursor.setPos(point.x())
            self._plot_cursor.show()

    def _apply_plot_appearance(self, *, spectrum=None) -> None:
        settings = self._plot_settings_model()
        if spectrum is None and getattr(self.spectrum_plot, "_vibrational_spectrum_id", None):
            spectrum = self.active_spectrum()
        reference_comparison = spectrum is None and self.active_spectrum() is None and bool(self.project.working_references)
        relative = reference_comparison or self.plot_control_bar.normalize_checkbox.isChecked()
        apply_spectrum_appearance(
            self.spectrum_plot, settings,
            title=spectrum.name if spectrum else DEFAULT_PLOT_TITLE,
            x_label="Raman shift" if reference_comparison or (
                spectrum is not None and spectrum.kind == SignalKind.RAMAN
            ) else "Wavenumber",
            y_label="Relative intensity" if relative else "Intensity",
            y_unit="" if relative else "a.u.",
            all_black=bool(self.project.ui.plot_view_settings.get("all_observed_black", False)),
            wavelength_context=self._wavelength_context(),
        )
        if not settings.cursor_vertical_line_visible and self._plot_cursor is not None:
            self._plot_cursor.hide()
