from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

import numpy as np
from scipy.ndimage import median_filter
from scipy.optimize import least_squares, nnls
from scipy.signal import find_peaks, savgol_filter

from finder_core.models import SignalKind
from vibrational_finder.band_detection import BandDetectionOptions, detect_bands
from vibrational_finder.models import ObservedSpectrum, SpectralBand, VibrationalMatchResult
from vibrational_finder.raman_gain import GainWindow
from vibrational_finder.services.preprocessing_service import estimate_background


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


@dataclass(frozen=True, slots=True)
class PhaseSubtractionFit:
    x: np.ndarray
    measured: np.ndarray
    corrected: np.ndarray
    bands: tuple[FittedBand, ...]
    background: np.ndarray


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
    """Fit independent peak widths jointly wherever their windows overlap."""
    centers = x[indices]
    step = float(np.median(np.diff(x)))
    sigma = np.maximum(widths / (2 * np.sqrt(2 * np.log(2))), step / 2)
    shift = np.minimum(np.maximum(sigma, step), 8.0)
    if len(centers) > 1:
        gap = np.diff(centers) * .4
        shift[:-1] = np.minimum(shift[:-1], gap)
        shift[1:] = np.minimum(shift[1:], gap)
    radii = 6 * widths + shift
    left = np.searchsorted(x, centers - radii)
    right = np.searchsorted(x, centers + radii, side="right")
    interval_order = np.argsort(left, kind="stable")
    start = 0
    while start < len(centers):
        first = interval_order[start]
        end, low, high = start + 1, left[first], right[first]
        while end < len(centers) and left[interval_order[end]] <= high:
            high = max(high, right[interval_order[end]])
            end += 1
        group = interval_order[start:end]
        points = x[low:high]
        heights = np.maximum(prominences[group], 0.25 * excess[indices[group]])
        initial = np.column_stack((heights, centers[group], sigma[group]))
        lower = np.column_stack((np.zeros(end - start), centers[group] - shift[group], sigma[group] * .45))
        upper = np.column_stack((np.maximum(excess[indices[group]], heights) * 2,
                                 centers[group] + shift[group], sigma[group] * 3.5))

        def terms(parameters):
            amplitude, center, spread = parameters.reshape(-1, 3).T
            distance = (points[:, None] - center) / spread
            shape = np.exp(-.5 * distance ** 2)
            return distance, shape, shape * amplitude, spread

        def residual(parameters):
            return terms(parameters)[2].sum(axis=1) - excess[low:high]

        def jacobian(parameters):
            distance, shape, model, spread = terms(parameters)
            return np.stack((shape, model * distance / spread, model * distance ** 2 / spread), axis=-1).reshape(len(points), -1)

        if len(points) >= initial.size:
            parameters = least_squares(residual, initial.ravel(), jac=jacobian,
                                       bounds=(lower.ravel(), upper.ravel()), max_nfev=70,
                                       x_scale="jac").x.reshape(-1, 3)
        else:
            parameters = initial
        for index, (amplitude, center, spread) in zip(group, parameters, strict=True):
            lo, hi = np.searchsorted(x, (center - 5 * spread, center + 5 * spread))
            window = slice(lo, hi)
            distance = abs(x[window] - center) / spread
            taper = np.clip(5 - distance, 0, 1)
            yield index, window, amplitude * np.exp(-.5 * distance ** 2) * taper
        start = end


def phase_reference_bands(
    spectrum: ObservedSpectrum, selected: Sequence[VibrationalMatchResult],
) -> dict[str, tuple[SpectralBand, ...]]:
    phases = {}
    for result in selected:
        candidate = result.candidate
        reference = candidate.reference
        bands = result.reference_bands or (candidate.band_set.bands if candidate.band_set else [])
        if not bands and reference is not None and reference.band_set is not None:
            bands = reference.band_set.bands
        if not bands and reference is not None and reference.x_unit == spectrum.x_unit:
            bands = detect_bands(reference, BandDetectionOptions(backend="scipy"))
        usable = tuple(band for band in bands if np.isfinite(band.position))
        if usable:
            phases[candidate.key] = usable
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


def _resolve_phase_bands(x, signal, phases):
    prominence_floor = max(float(np.ptp(signal)) * 0.015, 4 * float(np.median(np.abs(np.diff(signal)))))
    indices, props = find_peaks(signal, prominence=prominence_floor, width=1.5)
    samples = np.arange(x.size)
    widths = np.interp(props["right_ips"], samples, x) - np.interp(props["left_ips"], samples, x)
    return indices, widths, props["prominences"], _assign_phase_bands(x, indices, widths, phases)


def _assign_phase_bands(x, indices, widths, phases):
    centers = x[indices]
    claimed_by: list[dict[str, SpectralBand]] = [{} for _ in centers]
    uncertain: set[int] = set()
    tolerances = np.maximum(8.0, widths / 2)
    for phase, bands in phases.items():
        for band in bands:
            position = band.position
            if not np.isfinite(position) or not x[0] <= position <= x[-1]:
                continue
            distances = np.abs(centers - position)
            eligible = distances <= tolerances
            if not eligible.any():
                continue
            nearest = np.flatnonzero(eligible & np.isclose(distances, distances[eligible].min()))
            if len(nearest) == 1:
                i = nearest[0]
                existing = claimed_by[i].get(phase)
                if existing is None or distances[i] < abs(centers[i] - existing.position):
                    claimed_by[i][phase] = band
            else:
                uncertain.update(nearest.tolist())
    assignments = []
    for i, owners in enumerate(claimed_by):
        ambiguous = i in uncertain or len(owners) > 1
        key, band = next(iter(owners.items())) if len(owners) == 1 and not ambiguous else (None, None)
        assignments.append((key, band, ambiguous))
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
) -> PhaseSubtractionFit:
    """Model measured bands; subtract only uniquely assigned bands of selected phases."""
    if not subtract <= phases.keys():
        raise ValueError("A selected phase is no longer available.")
    x, y, sign, background = _subtraction_signal(observed)
    signal = sign * y - background
    indices, widths, prominences, assignments = _resolve_phase_bands(x, signal, phases)
    total_removed, total_retained = np.zeros_like(y), np.zeros_like(y)
    modeled = []
    curves = _fit_band_curves(x, signal, indices, widths, prominences)
    for i, segment, component in curves:
        key, band, ambiguous = assignments[i]
        removed = key in subtract
        (total_removed if removed else total_retained)[segment] += component
        modeled.append((i, key, band, ambiguous, removed, segment, component))
    # Reserve the estimated contribution of retained bands as well as background.
    # Noise can already lie below the baseline; never push it any further down.
    removal = np.minimum(total_removed, np.maximum(signal - total_retained, 0.0))
    fraction = np.divide(removal, total_removed, out=np.zeros_like(removal), where=total_removed > 0)
    fitted = []
    for i, key, band, ambiguous, removed, segment, component in sorted(modeled, key=lambda model: model[0]):
        contribution = component * fraction[segment] if removed else component
        fitted.append(FittedBand(
            float(x[indices[i]]), key, (band.mode or band.symmetry or "") if band else "",
            (band.assignment or "") if band else "", ambiguous, removed, float(sign * background[indices[i]]),
            tuple(x[segment].tolist()), tuple((sign * contribution).tolist()),
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
