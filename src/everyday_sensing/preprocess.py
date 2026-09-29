"""Anti-aliasing and decimation of vibration sensor recordings.

The Sonion VPU14DB01A is read by the firmware as 48 kHz PCM (a 24 kHz mode may be
adopted later) and analysed at 1,500 Hz. The sensor's datasheet resonance (typically
4.5 kHz, range 4 to 5 kHz) would fold into the 0 to 500 Hz band of interest under naive
decimation, so every recording is low-pass filtered with a linear-phase FIR before
downsampling.

Everything here operates on numpy arrays plus a sample rate; there is no file I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import signal

INT16_SCALE = 32768.0
"""Divisor that maps int16 PCM samples to floats in [-1, 1)."""

FREQZ_POINTS = 2**20 + 1
"""Frequency grid size (0 to fs_in/2 inclusive) used to measure a filter's response."""


@dataclass(frozen=True)
class FilterSpec:
    """Design inputs for the decimation filter.

    The defaults are the team decisions: 1,500 Hz output, 500 Hz passband edge, stopband
    starting at the output Nyquist (750 Hz) so the whole output band is alias-free,
    96 dB stopband attenuation (the 16-bit quantization floor) and 0.1 dB passband ripple.
    """

    fs_in: float
    fs_out: float = 1500.0
    passband_edge_hz: float = 500.0
    stopband_edge_hz: float = 750.0
    stopband_atten_db: float = 96.0
    passband_ripple_db: float = 0.1

    def __post_init__(self) -> None:
        if self.fs_in <= 0 or self.fs_out <= 0:
            raise ValueError("fs_in and fs_out must be positive")
        ratio = self.fs_in / self.fs_out
        if not float(ratio).is_integer() or ratio < 2:
            raise ValueError(
                f"fs_in / fs_out must be an integer >= 2, got {self.fs_in} / {self.fs_out}"
            )
        if not 0 < self.passband_edge_hz < self.stopband_edge_hz <= self.fs_out:
            raise ValueError(
                "require 0 < passband_edge_hz < stopband_edge_hz <= fs_out, got "
                f"{self.passband_edge_hz}, {self.stopband_edge_hz}, {self.fs_out}"
            )
        if self.stopband_atten_db <= 0 or self.passband_ripple_db <= 0:
            raise ValueError("stopband_atten_db and passband_ripple_db must be positive")

    @property
    def decimation_factor(self) -> int:
        return int(self.fs_in / self.fs_out)

    @property
    def cutoff_hz(self) -> float:
        """firwin cutoff (-6 dB point): the midpoint of the transition band."""
        return (self.passband_edge_hz + self.stopband_edge_hz) / 2


def kaiser_estimate(spec: FilterSpec) -> tuple[int, float]:
    """Kaiser-formula (numtaps, beta) for the spec, with numtaps forced odd.

    A Kaiser-window design has the same peak deviation in both bands, so the attenuation
    passed to kaiserord is the stricter of the stopband attenuation and the attenuation
    implied by the passband ripple.
    """
    ripple_delta = 10 ** (spec.passband_ripple_db / 20) - 1
    atten_db = max(spec.stopband_atten_db, -20 * math.log10(ripple_delta))
    width = (spec.stopband_edge_hz - spec.passband_edge_hz) / (spec.fs_in / 2)
    numtaps, beta = signal.kaiserord(atten_db, width)
    if numtaps % 2 == 0:
        numtaps += 1
    return numtaps, beta


def _meets_spec(response: dict, spec: FilterSpec) -> bool:
    return (
        response["passband_ripple_db"] <= spec.passband_ripple_db
        and response["stopband_atten_db"] >= spec.stopband_atten_db
    )


def design_decimation_filter(spec: FilterSpec) -> np.ndarray:
    """Design a symmetric, odd-length, linear-phase low-pass FIR for ``spec``.

    kaiserord's tap count is an estimate and can fall short of the requested attenuation,
    so the design starts at that estimate, keeps beta fixed, and adds 2 taps at a time
    until measured_response() meets the spec. Raises ValueError if the spec is not met by
    twice the kaiserord estimate.
    """
    numtaps, beta = kaiser_estimate(spec)
    max_numtaps = 2 * numtaps
    while numtaps <= max_numtaps:
        taps = signal.firwin(numtaps, spec.cutoff_hz, window=("kaiser", beta), fs=spec.fs_in)
        if _meets_spec(measured_response(taps, spec), spec):
            return taps
        numtaps += 2
    raise ValueError(f"spec not met with up to {max_numtaps} taps: {spec}")


def _check_taps(taps: np.ndarray) -> np.ndarray:
    taps = np.asarray(taps, dtype=np.float64)
    if taps.ndim != 1 or taps.size % 2 == 0:
        raise ValueError("taps must be a 1-D array with an odd number of coefficients")
    if not np.allclose(taps, taps[::-1], rtol=0, atol=1e-15):
        raise ValueError("taps must be symmetric (linear phase)")
    return taps


def decimate(x: np.ndarray, spec: FilterSpec, taps: np.ndarray | None = None) -> np.ndarray:
    """Low-pass filter and downsample ``x`` from spec.fs_in to spec.fs_out.

    ``x`` is 1-D int16 PCM (scaled by 1/32768) or float; the result is float64.
    resample_poly compensates the delay of the symmetric, odd-length FIR, so output
    sample ``m`` is aligned with input sample ``m * D`` (no net delay) and
    ``len(out) == ceil(len(x) / D)``.

    The signal is zero-padded beyond both ends, so the first and last
    edge_samples(taps, spec) output samples contain filter transients. They are not
    trimmed here; trim them before measuring. Run this per contiguous segment (see
    split_on_gaps) so the filter never spans a dropout.
    """
    x = np.asarray(x)
    if x.ndim != 1:
        raise ValueError("x must be 1-D")
    if x.dtype == np.int16:
        x = x.astype(np.float64) / INT16_SCALE
    elif np.issubdtype(x.dtype, np.floating):
        x = x.astype(np.float64)
    else:
        raise TypeError(f"x must be int16 or floating point, got {x.dtype}")
    taps = design_decimation_filter(spec) if taps is None else _check_taps(taps)
    return signal.resample_poly(x, 1, spec.decimation_factor, window=taps)


def edge_samples(taps: np.ndarray, spec: FilterSpec) -> int:
    """Output samples at each end of decimate()'s result affected by edge transients.

    Output sample ``m`` depends on input samples ``m*D - h`` to ``m*D + h`` with
    ``h = (numtaps - 1) / 2``, so at most ``ceil(h / D)`` output samples at each end
    reach past the input and see the zero padding.
    """
    half_len = (len(taps) - 1) // 2
    return math.ceil(half_len / spec.decimation_factor)


def measured_response(taps: np.ndarray, spec: FilterSpec, n_points: int = FREQZ_POINTS) -> dict:
    """Measure the achieved response of ``taps`` on a dense grid from 0 to fs_in/2.

    Returns numtaps, the max passband ripple (max |gain in dB| from 0 to the passband
    edge) and the min stopband attenuation (-max gain in dB from the stopband edge to
    fs_in/2).
    """
    freqs, h = signal.freqz(taps, worN=n_points, fs=spec.fs_in, include_nyquist=True)
    gain_db = 20 * np.log10(np.maximum(np.abs(h), np.finfo(np.float64).tiny))
    return {
        "numtaps": len(taps),
        "passband_ripple_db": float(np.max(np.abs(gain_db[freqs <= spec.passband_edge_hz]))),
        "stopband_atten_db": float(-np.max(gain_db[freqs >= spec.stopband_edge_hz])),
    }


def split_on_gaps(timestamps_s: np.ndarray, max_gap_s: float) -> list[slice]:
    """Split a recording into contiguous segments at timestamp gaps larger than max_gap_s.

    Returns slices into the sample array; a gap between samples ``i`` and ``i + 1`` ends
    one slice at ``i + 1`` and starts the next there. Segments shorter than the filter
    are returned as-is; dropping them is up to the caller.
    """
    t = np.asarray(timestamps_s, dtype=np.float64)
    if t.ndim != 1:
        raise ValueError("timestamps_s must be 1-D")
    if max_gap_s <= 0:
        raise ValueError("max_gap_s must be positive")
    if t.size == 0:
        return []
    dt = np.diff(t)
    if np.any(dt < 0):
        raise ValueError("timestamps_s must be non-decreasing")
    bounds = [0, *(np.flatnonzero(dt > max_gap_s) + 1).tolist(), t.size]
    return [slice(start, stop) for start, stop in zip(bounds[:-1], bounds[1:], strict=True)]
