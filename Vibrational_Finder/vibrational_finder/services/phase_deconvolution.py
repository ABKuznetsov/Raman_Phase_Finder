from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

import numpy as np
from scipy.ndimage import median_filter
from scipy.optimize import least_squares, nnls
from scipy.signal import find_peaks, peak_prominences, savgol_filter

from vibrational_finder.core.models import SignalKind
from vibrational_finder.band_detection import BandDetectionOptions, detect_bands
from vibrational_finder.cancellation import checkpoint
from vibrational_finder.models import ObservedSpectrum, ReferenceSpectrum, SpectralBand, VibrationalMatchResult
from vibrational_finder.raman_gain import GainWindow
from vibrational_finder.services.preprocessing_service import estimate_background


PHASE_FIT_MODEL_VERSION = 3


@dataclass(frozen=True, slots=True)
class PhaseDeconvolutionFit:
    x: np.ndarray
    measured: np.ndarray
    main: np.ndarray
    additional: np.ndarray
    scales: tuple[float, float]
    offset: float
    estimated_band_counts: tuple[int, int] | None = None
    ambiguous_band_count: int = 0

    def difference(self, additional_multiplier: float = 1.0) -> np.ndarray:
        """Measured minus the whole model, including the fitted constant."""
        return self.measured - (self.main + self.additional * additional_multiplier + self.offset)


@dataclass(frozen=True, slots=True)
class FittedBand:
    position: float
    phase_key: str | None
    mode: str
    assignment: str
    ambiguous: bool
    removed: bool
    baseline: float
    curve_x: tuple[float, ...]
    curve_y: tuple[float, ...]
    reference_position: float | None = None
    fwhm: float | None = None


def matched_reference_positions(bands: Sequence[FittedBand]) -> dict[str, set[float]]:
    positions: dict[str, set[float]] = {}
    for band in bands:
        if band.phase_key is not None and band.reference_position is not None:
            positions.setdefault(band.phase_key, set()).add(band.reference_position)
    return positions


@dataclass(frozen=True, slots=True)
class PhaseSubtractionFit:
    x: np.ndarray
    measured: np.ndarray
    corrected: np.ndarray
    bands: tuple[FittedBand, ...]
    background: np.ndarray
    model_version: int = PHASE_FIT_MODEL_VERSION


PHASE_SUBTRACTION_METADATA = "phase_subtraction"
BackgroundTrace = tuple[np.ndarray, np.ndarray]


def pack_subtraction(fit: PhaseSubtractionFit, phases: Mapping[str, tuple[str, str]]) -> str:
    return json.dumps({"phases": phases, "bands": [asdict(band) for band in fit.bands],
                       "background": [fit.x.tolist(), fit.background.tolist()]},
                      ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def unpack_subtraction(value: str) -> tuple[dict[str, tuple[str, str]], tuple[FittedBand, ...], BackgroundTrace | None]:
    try:
        data = json.loads(value)
        phases = {key: (str(name), str(color)) for key, (name, color) in data["phases"].items()}
        background = None
        if "background" in data:
            x, y = (np.asarray(axis, dtype=float) for axis in data["background"])
            if x.ndim == 1 and x.shape == y.shape and x.size > 1 and np.isfinite(x).all() and np.isfinite(y).all():
                if np.all(np.diff(x) > 0):
                    background = (x, y)
        return phases, tuple(FittedBand(**band) for band in data["bands"]), background
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}, (), None


def _ordered_points(spectrum: ObservedSpectrum) -> tuple[np.ndarray, np.ndarray]:
    x, y = np.asarray(spectrum.x, dtype=float), np.asarray(spectrum.y, dtype=float)
    if x.ndim != 1 or x.shape != y.shape or x.size < 2 or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError(f"{spectrum.name or spectrum.id} must contain at least two finite paired points.")
    order = np.argsort(x)
    x, y = x[order], y[order]
    if np.any(np.diff(x) == 0):
        raise ValueError(f"{spectrum.name or spectrum.id} contains duplicate coordinates.")
    return x, y


def _fit_band_curves(x, excess, indices, widths, prominences):
    """Fit each peak's height, center, FWHM and pseudo-Voigt mixing independently."""
    centers = x[indices]
    step = float(np.median(np.diff(x)))
    widths = np.maximum(widths, step)
    shift = np.minimum(np.maximum(widths / 2, step), 8.0)
    if len(centers) > 1:
        gap = np.diff(centers) * .4
        shift[:-1] = np.minimum(shift[:-1], gap)
        shift[1:] = np.minimum(shift[1:], gap)
    radii = 4 * widths + shift
    left = np.searchsorted(x, centers - radii)
    right = np.searchsorted(x, centers + radii, side="right")
    interval_order = np.argsort(left, kind="stable")
    start = 0
    while start < len(centers):
        checkpoint()
        first = interval_order[start]
        end, low, high = start + 1, left[first], right[first]
        while end < len(centers) and left[interval_order[end]] <= high:
            high = max(high, right[interval_order[end]])
            end += 1
        group = interval_order[start:end]
        points = x[low:high]
        heights = np.maximum(prominences[group], 0.25 * excess[indices[group]])
        initial = np.column_stack((heights, centers[group], widths[group], np.full(len(group), .25)))
        lower = np.column_stack((np.zeros(len(group)), centers[group] - shift[group],
                                 np.maximum(step / 2, widths[group] * .35), np.zeros(len(group))))
        upper = np.column_stack((np.maximum(excess[indices[group]], heights) * 2,
                                 centers[group] + shift[group], widths[group] * 4, np.ones(len(group))))
        local_x = (points - points.mean()) / (np.ptp(points) or 1.)
        background_basis, _ = np.linalg.qr(np.column_stack((np.ones_like(local_x), local_x, local_x ** 2)))

        def without_background(values):
            # A smooth local correction to the baseline must not become a fitted peak.
            return values - background_basis @ (background_basis.T @ values)

        def terms(parameters):
            amplitude, center, fwhm, mix = parameters.reshape(-1, 4).T
            distance = (points[:, None] - center) * 2 / fwhm
            gaussian, lorentzian = _peak_shapes(distance)
            return amplitude, fwhm, mix, distance, gaussian, lorentzian

        def residual(parameters):
            checkpoint()
            amplitude, _fwhm, mix, _distance, gaussian, lorentzian = terms(parameters)
            error = (_blend_peak(gaussian, lorentzian, mix) * amplitude).sum(axis=1) - excess[low:high]
            return without_background(error)

        def jacobian(parameters):
            amplitude, fwhm, mix, distance, gaussian, lorentzian = terms(parameters)
            slope = -2 * distance * amplitude * _blend_peak(np.log(2.) * gaussian, lorentzian ** 2, mix)
            partials = np.stack((
                _blend_peak(gaussian, lorentzian, mix), -2 * slope / fwhm,
                -distance * slope / fwhm, amplitude * (lorentzian - gaussian),
            ), axis=-1).reshape(len(points), -1)
            return without_background(partials)

        if len(points) >= initial.size + background_basis.shape[1]:
            parameters = least_squares(residual, initial.ravel(), jac=jacobian,
                                       bounds=(lower.ravel(), upper.ravel()), max_nfev=70,
                                       x_scale="jac").x.reshape(-1, 4)
        else:
            parameters = initial
        for index, (amplitude, center, fwhm, mix) in zip(group, parameters, strict=True):
            lo, hi = np.searchsorted(x, (center - 8 * fwhm, center + 8 * fwhm))
            window = slice(lo, hi)
            distance = (x[window] - center) * 2 / fwhm
            taper = np.clip(8 - abs(distance) / 2, 0, 1)
            curve = amplitude * _blend_peak(*_peak_shapes(distance), mix) * taper
            yield index, window, curve, float(fwhm)
        start = end


def _peak_shapes(distance):
    squared = distance ** 2
    return np.exp(-np.log(2.) * squared), 1 / (1 + squared)


def _blend_peak(gaussian, lorentzian, mix):
    return gaussian + mix * (lorentzian - gaussian)


def phase_reference_input(
    result: VibrationalMatchResult,
) -> tuple[Sequence[SpectralBand], ReferenceSpectrum | None]:
    candidate = result.candidate
    reference = candidate.reference
    bands = result.reference_bands or (candidate.band_set.bands if candidate.band_set else [])
    if not bands and reference is not None and reference.band_set is not None:
        bands = reference.band_set.bands
    return bands, reference if not bands else None


def phase_reference_bands(
    spectrum: ObservedSpectrum, selected: Sequence[VibrationalMatchResult],
) -> dict[str, tuple[SpectralBand, ...]]:
    phases = {}
    for result in selected:
        checkpoint()
        bands, reference = phase_reference_input(result)
        if reference is not None and reference.x_unit == spectrum.x_unit:
            bands = detect_bands(reference, BandDetectionOptions(backend="scipy"))
        usable = tuple(band for band in bands if np.isfinite(band.position))
        if usable:
            phases[result.candidate.key] = usable
    return phases


def _subtraction_signal(observed: ObservedSpectrum):
    if observed.x_unit.strip().lower() not in {"cm-1", "cm^-1", "cm⁻¹", "1/cm"}:
        raise ValueError("Band-based deconvolution requires measured coordinates in cm⁻¹.")
    x, y = _ordered_points(observed)
    sign = -1 if observed.kind == SignalKind.FTIR and observed.y_unit.strip().lower() in {
        "%t", "%r", "transmittance", "reflectance",
    } else 1
    signed_y = sign * y
    background = estimate_background(x, signed_y, method="arpls")
    return x, y, sign, background


def _reference_positions(phases, phase_offsets, matched=()):
    offsets = phase_offsets or {}
    return np.asarray([band.position + offsets.get(key, 0.) for key, bands in phases.items()
                       for band in bands if np.isfinite(band.position) and (key, band.position) not in matched])


def _peak_candidates(x, signal, prominence_floor):
    indices, props = find_peaks(signal, prominence=prominence_floor, width=1.5)
    samples = np.arange(x.size)
    widths = np.interp(props["right_ips"], samples, x) - np.interp(props["left_ips"], samples, x)
    return indices, widths, props["prominences"]


def _measured_prominence_floor(signal):
    differences = np.diff(signal)
    noise = 1.4826 / np.sqrt(2.) * np.median(np.abs(differences - np.median(differences)))
    return max(5. * float(noise), .0015 * float(np.ptp(signal)), np.finfo(float).eps)


def _resolve_phase_bands(x, signal, phases, phase_offsets=None, *, compare_unshifted=False):
    floor = _measured_prominence_floor(signal)
    indices, widths, prominences = _peak_candidates(x, signal, floor)
    nominal = _reference_positions(phases, phase_offsets)
    if compare_unshifted:
        nominal = np.r_[nominal, _reference_positions(phases, None)]
    near_reference = np.any(np.abs(x[indices, None] - nominal) <= 8., axis=1)
    span = float(np.ptp(signal))
    # Use local contrast for weak bands so slow baseline drift does not count as a peak.
    step = float(np.median(np.diff(x)))
    window = 2 * max(2, int(np.ceil(12. / step))) + 1
    local_contrast = peak_prominences(signal, indices, wlen=window)[0]
    weak = near_reference & (local_contrast >= max(floor, .003 * span))
    measured = (prominences >= .015 * span) | (local_contrast >= max(1.4 * floor, .0075 * span))
    keep = (weak | measured) & (signal[indices] >= floor)
    return indices[keep], widths[keep], prominences[keep]


def _validated_phase_offsets(x, indices, phases, proposed):
    """A shift found in residual search is a hypothesis, not a fit constraint."""
    centers = x[indices]
    if centers.size < 2:
        return {}

    def support(bands, offset):
        references = np.asarray([band.position + offset for band in bands
                                 if x[0] <= band.position + offset <= x[-1]])
        if not references.size:
            return 0, 0.
        distances = np.abs(centers[:, None] - references)
        errors = np.full(centers.size, 5.)
        np.minimum.at(errors, distances.argmin(axis=0), distances.min(axis=0))
        return np.count_nonzero(errors < 5.), float(np.sum(1 - (errors / 5.) ** 2))

    offsets = {}
    for key, shift in proposed.items():
        checkpoint()
        if key not in phases or not np.isfinite(shift) or not shift:
            continue
        base_count, base_quality = support(phases[key], 0.)
        shifted_count, shifted_quality = support(phases[key], shift)
        if shifted_count >= max(2, base_count) and shifted_quality > base_quality + .25:
            offsets[key] = shift
    return offsets


def _add_supported_shoulders(x, signal, phases, phase_offsets, indices, widths, prominences, curves):
    if not curves:
        return None
    existing = _assign_phase_bands(x, indices, widths, phases, phase_offsets)
    matched = {(key, band.position) for key, band, _ in existing if band is not None}
    nominal = _reference_positions(phases, phase_offsets, matched)
    if not nominal.size:
        return None
    model = np.zeros_like(signal)
    for _, window, curve, _ in curves:
        model[window] += curve
    floor = _measured_prominence_floor(signal)
    residual = signal - model
    extra, extra_widths, extra_prominences = _peak_candidates(x, residual, floor)
    gap = max(1., 2. * float(np.median(np.diff(x))))
    # Require a distinct, positive shoulder of an existing lobe at an unused card line.
    keep = ((residual[extra] >= floor) & (model[extra] >= .12 * extra_prominences)
            & np.any(np.abs(x[extra, None] - nominal) <= 5., axis=1)
            & np.all(np.abs(x[extra, None] - x[indices]) >= gap, axis=1))
    if not keep.any():
        return None
    new_indices = np.r_[indices, extra[keep]]
    new_widths = np.r_[widths, extra_widths[keep]]
    new_prominences = np.r_[prominences, extra_prominences[keep]]
    assignments = _assign_phase_bands(x, new_indices, new_widths, phases, phase_offsets)
    chosen = np.asarray([i < len(indices) or (band is not None and (key, band.position) not in matched)
                         for i, (key, band, _) in enumerate(assignments)])
    if not np.any(chosen[len(indices):]):
        return None
    order = np.flatnonzero(chosen)
    order = order[np.argsort(new_indices[order])]
    return new_indices[order], new_widths[order], new_prominences[order]


def _assign_phase_bands(x, indices, widths, phases, phase_offsets=None):
    centers = x[indices]
    claimed_by: list[dict[str, tuple[float, SpectralBand, bool]]] = [{} for _ in centers]
    tolerances = np.maximum(8.0, widths / 2)
    for phase, bands in phases.items():
        checkpoint()
        offset = phase_offsets.get(phase, 0.) if phase_offsets else 0.
        for band in bands:
            position = band.position + offset
            if not np.isfinite(position) or not x[0] <= position <= x[-1]:
                continue
            distances = np.abs(centers - position)
            eligible = distances <= tolerances
            if not eligible.any():
                continue
            nearest = np.flatnonzero(eligible & np.isclose(distances, distances[eligible].min()))
            for i in nearest:
                distance, unique = float(distances[i]), nearest.size == 1
                existing = claimed_by[i].get(phase)
                if existing is None or (distance, not unique) < (existing[0], not existing[2]):
                    claimed_by[i][phase] = (distance, band, unique)
    assignments = []
    step = float(np.median(np.diff(x)))
    for width, owners in zip(widths, claimed_by, strict=True):
        if not owners:
            assignments.append((None, None, False))
            continue
        ranked = sorted(owners.items(), key=lambda item: item[1][0])
        key, (distance, band, unique) = ranked[0]
        # A worse match is not a veto; keep a shared band when the difference
        # is too small relative to sampling and the measured width to resolve.
        margin = max(1., 1.5 * step, .25 * width)
        ambiguous = not unique or (len(ranked) > 1 and ranked[1][1][0] - distance <= margin)
        assignments.append((None, None, True) if ambiguous else (key, band, False))
    return assignments


def retained_raman_evidence(
    observed: ObservedSpectrum, phases: Mapping[str, Sequence[SpectralBand]],
) -> tuple[list[SpectralBand], GainWindow]:
    """Find independently supported Raman bands before subtracting known phases.

    Do not redetect on the corrected trace: a fitted peak's remaining shoulders or
    newly prominent noise must not be counted as independent bands.
    """
    if observed.x_unit.strip().lower() not in {"cm-1", "cm^-1", "cm⁻¹", "1/cm"}:
        raise ValueError("Band-based deconvolution requires measured coordinates in cm⁻¹.")
    x, y = _ordered_points(observed)
    measured = (float(x[0]), float(x[-1]))
    if len(x) < 7:
        return [], GainWindow(measured)
    dx = float(np.median(np.diff(x)))
    despiked = median_filter(y, size=3)
    background = estimate_background(x, despiked, method="arpls")
    signal = despiked - background
    width = min(21, max(5, int(round(2.4 / dx)) | 1))
    wide = min(31, max(width + 2, int(round(4 / dx)) | 1))
    maximum = len(x) if len(x) % 2 else len(x) - 1
    light = savgol_filter(signal, min(width, maximum), 2)
    broad = savgol_filter(signal, min(wide, maximum), 2)
    indices, props = find_peaks(light, prominence=0., width=max(2.4, 1.4 / dx))
    stable, _ = find_peaks(broad)
    raw = y - background
    noise_floor = np.finfo(float).eps * max(float(np.ptp(y)), 1.)
    noise_factor = 1.4826 / np.sqrt(2)
    threshold = 5.5

    def local_trace(position: float) -> np.ndarray:
        lo, hi = np.searchsorted(x, [position - max(28., 12 * dx), position + max(28., 12 * dx)])
        return raw[lo:hi]

    def noise_from_diffs(diffs: np.ndarray) -> float:
        return max(float(noise_factor * np.median(np.abs(diffs - np.median(diffs)))), noise_floor)

    good = []
    for j, i in enumerate(indices):
        noise = noise_from_diffs(np.diff(local_trace(x[i])))
        consistent_center = np.any(abs(x[stable] - x[i]) < max(2.2, 2 * dx))
        if props["prominences"][j] >= threshold * noise and signal[i] > 0 and consistent_center:
            good.append(j)
    indices = indices[good]
    widths = np.interp(props["right_ips"][good], np.arange(len(x)), x) - np.interp(props["left_ips"][good], np.arange(len(x)), x)
    assignments = _assign_phase_bands(x, indices, widths, phases)
    bands, masked = [], []
    for i, fwhm, prominence, (key, _band, ambiguous) in zip(indices, widths, props["prominences"][good], assignments, strict=True):
        position = float(x[i])
        if key is None and not ambiguous:
            # Adjacent differences underestimate correlated background. Check on
            # the scale of this peak as well, without discarding weak evidence.
            lag = max(2, int(round(max(fwhm, 3.) / dx)))
            inner = 1.5 * fwhm
            outer = max(28., 3. * fwhm, inner + (lag + 8) * dx)
            lo, left, right, hi = np.searchsorted(x, [position - outer, position - inner,
                                                     position + inner, position + outer])
            # Neither flank includes the candidate itself; never form a
            # difference across the excluded peak.
            diffs = np.concatenate((raw[lo + lag:left] - raw[lo:left - lag] if left - lo > lag else [],
                                    raw[right + lag:hi] - raw[right:hi - lag] if hi - right > lag else []))
            strong = diffs.size >= 6 and prominence >= threshold * noise_from_diffs(diffs)
            bands.append(SpectralBand(position=position, intensity=1., width=float(fwhm),
                                      prominence=float(prominence), confidence=1. if strong else .25))
        else:
            half_width = float(np.clip(fwhm / 2, 1.7, 6.))
            masked.append((position - half_width, position + half_width))
    return bands, GainWindow(measured, tuple(masked))


def fit_known_phase_subtraction(
    observed: ObservedSpectrum,
    phases: Mapping[str, Sequence[SpectralBand]],
    subtract: set[str],
    *,
    phase_offsets: Mapping[str, float] | None = None,
) -> PhaseSubtractionFit:
    """Model measured bands; subtract only uniquely assigned bands of selected phases."""
    if not subtract <= phases.keys():
        raise ValueError("A selected phase is no longer available.")
    x, y, sign, background = _subtraction_signal(observed)
    checkpoint()
    signal = sign * y - background
    if phase_offsets:
        evidence, _, _ = _resolve_phase_bands(x, signal, phases, phase_offsets, compare_unshifted=True)
        phase_offsets = _validated_phase_offsets(x, evidence, phases, phase_offsets)
    indices, widths, prominences = _resolve_phase_bands(x, signal, phases, phase_offsets)
    curves = list(_fit_band_curves(x, signal, indices, widths, prominences))
    shoulders = _add_supported_shoulders(x, signal, phases, phase_offsets, indices, widths, prominences, curves)
    if shoulders is not None:
        indices, widths, prominences = shoulders
        curves = list(_fit_band_curves(x, signal, indices, widths, prominences))
    assignments = _assign_phase_bands(x, indices, widths, phases, phase_offsets)
    total_removed, total_retained = np.zeros_like(y), np.zeros_like(y)
    modeled = []
    for i, segment, component, fwhm in curves:
        key, band, ambiguous = assignments[i]
        removed = key in subtract
        (total_removed if removed else total_retained)[segment] += component
        modeled.append((i, key, band, ambiguous, removed, segment, component, fwhm))
    # Reserve the estimated contribution of retained bands as well as background.
    # Noise can already lie below the baseline; never push it any further down.
    removal = np.minimum(total_removed, np.maximum(signal - total_retained, 0.0))
    fraction = np.divide(removal, total_removed, out=np.zeros_like(removal), where=total_removed > 0)
    fitted = []
    for i, key, band, ambiguous, removed, segment, component, fwhm in sorted(modeled, key=lambda model: model[0]):
        contribution = component * fraction[segment] if removed else component
        fitted.append(FittedBand(
            float(x[indices[i]]), key, (band.mode or band.symmetry or "") if band else "",
            (band.assignment or "") if band else "", ambiguous, removed, float(sign * background[indices[i]]),
            tuple(x[segment].tolist()), tuple((sign * contribution).tolist()),
            reference_position=float(band.position) if band is not None else None,
            fwhm=fwhm,
        ))
    return PhaseSubtractionFit(x, y, y - sign * removal, tuple(fitted), sign * background)


def estimate_phase_deconvolution_from_bands(
    observed: ObservedSpectrum,
    main_bands: Sequence[SpectralBand],
    additional_bands: Sequence[SpectralBand],
) -> PhaseDeconvolutionFit:
    phases = {"main": main_bands, "additional": additional_bands}
    if any(not any(np.isfinite(band.position) for band in bands) for bands in phases.values()):
        raise ValueError("Both phases need at least one usable reference band or a measured profile.")
    fit = fit_known_phase_subtraction(observed, phases, set(phases))
    x, y = fit.x, fit.measured
    components = np.zeros((2, x.size))
    counts = [0, 0]
    for band in fit.bands:
        if band.phase_key is not None:
            phase = 0 if band.phase_key == "main" else 1
            components[phase, np.searchsorted(x, band.curve_x)] += band.curve_y
            counts[phase] += 1
    if not any(counts):
        raise ValueError("No resolved band could be assigned uniquely; shared bands were left unchanged.")
    untouched = y[np.all(components == 0.0, axis=0)]
    offset = float(np.median(untouched)) if untouched.size else 0.0
    return PhaseDeconvolutionFit(
        x, y, components[0], components[1], (float(counts[0] > 0), float(counts[1] > 0)), offset,
        estimated_band_counts=(counts[0], counts[1]),
        ambiguous_band_count=sum(band.ambiguous for band in fit.bands),
    )


def fit_phase_deconvolution(
    observed: ObservedSpectrum,
    main_reference: ObservedSpectrum,
    additional_reference: ObservedSpectrum,
) -> PhaseDeconvolutionFit:
    """Fit one constant and two nonnegative profile scales over common measured coordinates."""
    traces = (observed, main_reference, additional_reference)
    if len({item.id for item in traces}) != 3:
        raise ValueError("Choose two distinct phase references other than the measured spectrum.")
    if any(item.kind != observed.kind for item in traces[1:]):
        raise ValueError("Both references must use the same method as the measured spectrum.")
    if any((item.x_unit, item.y_unit) != (observed.x_unit, observed.y_unit) for item in traces[1:]):
        raise ValueError("Both references must use the same coordinate and intensity units.")

    x, y = _ordered_points(observed)
    reference_data = [_ordered_points(item) for item in traces[1:]]
    lower = max(rx[0] for rx, _ in reference_data)
    upper = min(rx[-1] for rx, _ in reference_data)
    mask = (x >= lower) & (x <= upper)
    if np.count_nonzero(mask) < 4:
        raise ValueError("The spectra need at least four measured points in their common range.")
    x, y = x[mask], y[mask]
    profiles = np.column_stack([np.interp(x, rx, ry) for rx, ry in reference_data])
    means = profiles.mean(axis=0)
    centered = profiles - means
    norms = np.linalg.norm(centered, axis=0)
    if np.any(norms <= 0) or not np.all(np.isfinite(norms)):
        raise ValueError("Both references must have varying intensity within the common range.")
    design = centered / norms
    # Close to proportional shapes cannot support distinct phase assignments reliably.
    if np.linalg.matrix_rank(design, tol=1e-6) < 2:
        raise ValueError("The reference profiles are too similar to separate in the common range.")
    mean_y = float(y.mean())
    coefficients, _ = nnls(design, y - mean_y)
    scales = coefficients / norms
    offset = mean_y - float(means @ scales)
    return PhaseDeconvolutionFit(
        x, y, profiles[:, 0] * scales[0], profiles[:, 1] * scales[1],
        (float(scales[0]), float(scales[1])), offset,
    )
