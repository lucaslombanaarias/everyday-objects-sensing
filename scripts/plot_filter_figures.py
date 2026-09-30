"""Slide figures for the 48 kHz decimation filter.

Run as ``python scripts/plot_filter_figures.py``. Writes two 300 dpi PNGs to docs/figures/:

- filter_response.png: magnitude response of the designed taps against the spec.
- aliasing_demo.png: a 4,200 Hz tone decimated naively (folds into the kept band) and
  through preprocess.decimate() (suppressed by the filter).

Only the spec lines, the datasheet resonance band and the test tone frequency are fixed
inputs; every other number on the figures is computed from the taps at runtime, with the
same measured_response() the tests use.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib as mpl
import numpy as np
from matplotlib.figure import Figure
from scipy import signal

from everyday_sensing.filter_report import (
    BAND_COLOR,
    GRID_COLOR,
    RESONANCE_BAND_HZ,
    SERIES_COLOR,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from everyday_sensing.preprocess import (
    FREQZ_POINTS,
    FilterSpec,
    decimate,
    design_decimation_filter,
    edge_samples,
    measured_response,
)

FS_IN_HZ = 48000.0
TEST_TONE_HZ = 4200.0
TONE_AMPLITUDE = 0.5
KEPT_OUTPUT_SAMPLES = 6000
"""Output samples kept after trimming transients (4 s at 1,500 Hz). A whole number of
cycles of both the input tone and its alias, so the Hann-windowed FFT has no scalloping."""
OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "figures"

FONT_PT = 16
SPEC_COLOR = "#3d3c39"
FILTERED_COLOR = SERIES_COLOR
NAIVE_COLOR = "#c2410c"

mpl.rcParams.update(
    {
        "font.size": FONT_PT,
        "axes.labelsize": FONT_PT,
        "xtick.labelsize": FONT_PT,
        "ytick.labelsize": FONT_PT,
        "legend.fontsize": FONT_PT,
        "axes.labelcolor": TEXT_PRIMARY,
        "xtick.color": TEXT_SECONDARY,
        "ytick.color": TEXT_SECONDARY,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
    }
)


def _style_axes(ax) -> None:
    ax.grid(color=GRID_COLOR, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color(GRID_COLOR)


def plot_response(path: Path, spec: FilterSpec, taps: np.ndarray, response: dict) -> None:
    freqs, h = signal.freqz(taps, worN=FREQZ_POINTS, fs=spec.fs_in, include_nyquist=True)
    gain_db = 20 * np.log10(np.maximum(np.abs(h), np.finfo(np.float64).tiny))

    fig = Figure(figsize=(7, 4.5), layout="constrained")
    ax = fig.subplots()
    ax.axvspan(*RESONANCE_BAND_HZ, color=BAND_COLOR, alpha=0.15, linewidth=0)
    ax.text(
        sum(RESONANCE_BAND_HZ) / 2,
        -65,
        "VPU resonance\n(datasheet)",
        rotation=90,
        ha="center",
        va="center",
        color=NAIVE_COLOR,
    )
    ax.plot(freqs, gain_db, color=FILTERED_COLOR, linewidth=1.6, linestyle="-")

    spec_line = {"color": SPEC_COLOR, "linestyle": "--", "linewidth": 1.2}
    ax.axvline(spec.passband_edge_hz, **spec_line)
    ax.axvline(spec.stopband_edge_hz, **spec_line)
    ax.axhline(-spec.stopband_atten_db, **spec_line)
    ax.text(
        spec.passband_edge_hz - 60,
        -50,
        f"{spec.passband_edge_hz:g} Hz",
        rotation=90,
        ha="right",
        va="center",
        color=SPEC_COLOR,
    )
    ax.text(
        spec.stopband_edge_hz + 60,
        -45,
        f"{spec.stopband_edge_hz:g} Hz",
        rotation=90,
        ha="left",
        va="center",
        color=SPEC_COLOR,
    )
    ax.text(
        RESONANCE_BAND_HZ[0] - 100,
        -spec.stopband_atten_db + 2,
        f"spec -{spec.stopband_atten_db:g} dB",
        ha="right",
        va="bottom",
        color=SPEC_COLOR,
    )
    ax.text(
        1250,
        -3,
        f"From filter taps ({response['numtaps']} taps):\n"
        f"passband ripple {response['passband_ripple_db']:.2g} dB\n"
        f"min stopband atten. {response['stopband_atten_db']:.2f} dB",
        ha="left",
        va="top",
        color=TEXT_PRIMARY,
        linespacing=1.3,
    )

    ax.set_xlim(0, 6000)
    ax.set_ylim(-130, 5)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Magnitude (dB)")
    _style_axes(ax)
    fig.savefig(path, dpi=300, metadata={"Software": None})


def _amplitude_spectrum(x: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """Hann-windowed single-sided amplitude spectrum; an on-bin tone of amplitude A reads A."""
    window = np.hanning(len(x))
    spectrum = 2 * np.abs(np.fft.rfft(x * window)) / np.sum(window)
    return np.fft.rfftfreq(len(x), d=1 / fs), spectrum


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def aliasing_spectra(spec: FilterSpec, taps: np.ndarray) -> dict:
    """Spectra of a TEST_TONE_HZ tone after naive and filtered decimation, in dB re input.

    Both outputs are trimmed by edge_samples() at each end so they cover the same span,
    and the reference is the input tone measured over that same span at fs_in.
    """
    factor = spec.decimation_factor
    k = edge_samples(taps, spec)
    n_in = (KEPT_OUTPUT_SAMPLES + 2 * k) * factor
    t = np.arange(n_in) / spec.fs_in
    x = TONE_AMPLITUDE * np.sin(2 * np.pi * TEST_TONE_HZ * t)

    kept = slice(k, k + KEPT_OUTPUT_SAMPLES)
    naive = x[::factor][kept]
    filtered = decimate(x, spec, taps)[kept]
    reference = x[k * factor : (k + KEPT_OUTPUT_SAMPLES) * factor]

    _, ref_spectrum = _amplitude_spectrum(reference, spec.fs_in)
    ref_amplitude = np.max(ref_spectrum)
    tiny = np.finfo(np.float64).tiny
    freqs, naive_spectrum = _amplitude_spectrum(naive, spec.fs_out)
    _, filtered_spectrum = _amplitude_spectrum(filtered, spec.fs_out)
    naive_db = 20 * np.log10(np.maximum(naive_spectrum / ref_amplitude, tiny))
    filtered_db = 20 * np.log10(np.maximum(filtered_spectrum / ref_amplitude, tiny))

    peak = int(np.argmax(naive_db))
    folded = abs(TEST_TONE_HZ - spec.fs_out * round(TEST_TONE_HZ / spec.fs_out))
    return {
        "freqs": freqs,
        "naive_db": naive_db,
        "filtered_db": filtered_db,
        "alias_hz": float(freqs[peak]),
        "expected_alias_hz": folded,
        "naive_alias_db": float(naive_db[peak]),
        "filtered_alias_db": float(filtered_db[peak]),
        "filter_gain_db": float(
            20 * np.log10(np.abs(signal.freqz(taps, worN=[TEST_TONE_HZ], fs=spec.fs_in)[1][0]))
        ),
        "edge_samples": k,
    }


def plot_aliasing(path: Path, spec: FilterSpec, taps: np.ndarray, spectra: dict) -> None:
    freqs = spectra["freqs"]
    alias_hz = spectra["alias_hz"]
    y_floor = 20 * np.floor((spectra["filtered_alias_db"] - 25) / 20)

    fig = Figure(figsize=(7, 6), layout="constrained")
    ax_naive, ax_filt = fig.subplots(2, 1, sharex=True, sharey=True)
    panels = [
        (
            ax_naive,
            spectra["naive_db"],
            spectra["naive_alias_db"],
            NAIVE_COLOR,
            "--",
            "o",
            f"Naive: every {_ordinal(spec.decimation_factor)} sample",
        ),
        (
            ax_filt,
            spectra["filtered_db"],
            spectra["filtered_alias_db"],
            FILTERED_COLOR,
            "-",
            "s",
            f"Repo filter ({len(taps)} taps) + decimate",
        ),
    ]
    for ax, spectrum_db, alias_db, color, linestyle, marker, title in panels:
        ax.plot(freqs, spectrum_db, color=color, linestyle=linestyle, linewidth=1.8)
        ax.plot([alias_hz], [alias_db], marker=marker, color=color, markersize=9)
        ax.axvline(spec.passband_edge_hz, color=SPEC_COLOR, linestyle=":", linewidth=1.2)
        ax.text(
            0.99,
            0.97,
            f"{title}\nsynthetic 4.2 kHz test tone",
            transform=ax.transAxes,
            ha="right",
            va="top",
            color=TEXT_PRIMARY,
            linespacing=1.3,
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 2},
        )
        ax.annotate(
            f"{alias_hz:.0f} Hz, {alias_db:.1f} dB",
            xy=(alias_hz, alias_db),
            xytext=(alias_hz - 20, alias_db),
            ha="right",
            va="center",
            color=color,
        )
        ax.set_ylabel("Level (dB re input)")
        _style_axes(ax)

    ax_filt.text(
        spec.passband_edge_hz + 8,
        y_floor + 3,
        f"{spec.passband_edge_hz:g} Hz",
        ha="left",
        va="bottom",
        color=SPEC_COLOR,
    )
    ax_filt.set_xlim(0, spec.stopband_edge_hz)
    ax_filt.set_ylim(y_floor, 10)
    ax_filt.set_xlabel("Frequency (Hz)")
    fig.savefig(path, dpi=300, metadata={"Software": None})


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    spec = FilterSpec(fs_in=FS_IN_HZ)
    taps = design_decimation_filter(spec)
    response = measured_response(taps, spec)
    spectra = aliasing_spectra(spec, taps)

    plot_response(OUT_DIR / "filter_response.png", spec, taps, response)
    plot_aliasing(OUT_DIR / "aliasing_demo.png", spec, taps, spectra)

    print(f"taps: {response['numtaps']}")
    print(f"passband ripple: {response['passband_ripple_db']:.6g} dB")
    print(f"min stopband attenuation: {response['stopband_atten_db']:.4f} dB")
    print(
        f"naive alias: {spectra['alias_hz']:g} Hz at {spectra['naive_alias_db']:.2f} dB "
        f"(expected fold {spectra['expected_alias_hz']:g} Hz)"
    )
    print(
        f"filtered at alias: {spectra['filtered_alias_db']:.2f} dB "
        f"(|H({TEST_TONE_HZ:g} Hz)| = {spectra['filter_gain_db']:.2f} dB)"
    )
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
