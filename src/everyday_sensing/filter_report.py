"""Write the decimation filter's measured response for each supported input rate.

Run as ``python -m everyday_sensing.filter_report [--out-dir DIR]``. For each input
rate it writes response_<fs_in>.csv (freq_hz, magnitude_db) and response_<fs_in>.png,
plus one summary.json with the spec inputs, the kaiserord estimate and the measured
response of the final design.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
from matplotlib.figure import Figure
from scipy import signal

from everyday_sensing.preprocess import (
    FilterSpec,
    design_decimation_filter,
    edge_samples,
    kaiser_estimate,
    measured_response,
)

INPUT_RATES_HZ = (48000, 24000)
RESONANCE_BAND_HZ = (4000.0, 5000.0)
"""Datasheet range of the sensor's resonant peak."""
CSV_STEP_HZ = 1.0
"""Frequency spacing of the CSV and plot. The summary figures use the dense grid in
measured_response()."""
DEFAULT_OUT_DIR = Path(__file__).resolve().parents[2] / "results" / "decimation_filter"

# Chart colors: one data series plus recessive annotation ink.
SERIES_COLOR = "#2a78d6"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID_COLOR = "#e4e3df"
BAND_COLOR = "#eb6834"


def response_on_grid(taps: np.ndarray, fs_in: float) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.arange(0.0, fs_in / 2 + CSV_STEP_HZ / 2, CSV_STEP_HZ)
    _, h = signal.freqz(taps, worN=freqs, fs=fs_in)
    gain_db = 20 * np.log10(np.maximum(np.abs(h), np.finfo(np.float64).tiny))
    return freqs, gain_db


def write_csv(path: Path, freqs: np.ndarray, gain_db: np.ndarray) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["freq_hz", "magnitude_db"])
        writer.writerows(zip(freqs.tolist(), gain_db.tolist(), strict=True))


def _style_axes(ax, xlabel: str) -> None:
    ax.grid(color=GRID_COLOR, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color(GRID_COLOR)
    ax.tick_params(colors=TEXT_SECONDARY)
    ax.set_ylabel("Magnitude (dB)", color=TEXT_SECONDARY)
    ax.set_xlabel(xlabel, color=TEXT_SECONDARY)


def _mark_spec(ax, spec: FilterSpec, response: dict) -> None:
    ax.axvline(
        spec.passband_edge_hz,
        color=TEXT_SECONDARY,
        linestyle="--",
        linewidth=0.9,
        label=f"passband edge {spec.passband_edge_hz:g} Hz",
    )
    ax.axvline(
        spec.stopband_edge_hz,
        color=TEXT_SECONDARY,
        linestyle="-.",
        linewidth=0.9,
        label=f"stopband edge {spec.stopband_edge_hz:g} Hz",
    )
    ax.axhline(
        -spec.stopband_atten_db,
        color=TEXT_SECONDARY,
        linestyle=":",
        linewidth=0.9,
        label=f"spec \u2212{spec.stopband_atten_db:g} dB "
        f"(measured \u2212{response['stopband_atten_db']:.2f} dB)",
    )


def plot_response(
    path: Path, spec: FilterSpec, freqs: np.ndarray, gain_db: np.ndarray, response: dict
) -> None:
    fig = Figure(figsize=(9, 10), layout="constrained")
    ax_full, ax_edge, ax_pass = fig.subplots(3, 1, gridspec_kw={"height_ratios": [3, 2, 2]})
    fig.suptitle(
        f"Decimation filter, {spec.fs_in:g} Hz \u2192 {spec.fs_out:g} Hz "
        f"(D = {spec.decimation_factor}, {response['numtaps']} taps)",
        color=TEXT_PRIMARY,
    )
    floor_db = -spec.stopband_atten_db - 60

    ax_full.plot(freqs, gain_db, color=SERIES_COLOR, linewidth=1.0)
    ax_full.axvspan(
        *RESONANCE_BAND_HZ,
        color=BAND_COLOR,
        alpha=0.2,
        linewidth=0,
        label="sensor resonance 4\u20135 kHz (datasheet)",
    )
    _mark_spec(ax_full, spec, response)
    ax_full.set_xlim(0, spec.fs_in / 2)
    ax_full.set_ylim(floor_db, 10)
    _style_axes(ax_full, "Frequency (Hz)")
    ax_full.legend(loc="upper right", fontsize=8, frameon=False, labelcolor=TEXT_SECONDARY)

    in_edge = freqs <= spec.fs_out
    ax_edge.plot(freqs[in_edge], gain_db[in_edge], color=SERIES_COLOR, linewidth=1.2)
    _mark_spec(ax_edge, spec, response)
    ax_edge.set_xlim(0, spec.fs_out)
    ax_edge.set_ylim(floor_db, 10)
    _style_axes(ax_edge, f"Frequency (Hz), 0 to {spec.fs_out:g} Hz (transition band)")

    in_pass = freqs <= spec.passband_edge_hz
    ax_pass.axhspan(
        -spec.passband_ripple_db,
        spec.passband_ripple_db,
        color=GRID_COLOR,
        alpha=0.6,
        label=f"spec \u00b1{spec.passband_ripple_db:g} dB "
        f"(measured max {response['passband_ripple_db']:.2g} dB)",
    )
    ax_pass.plot(freqs[in_pass], gain_db[in_pass], color=SERIES_COLOR, linewidth=1.2)
    ax_pass.set_xlim(0, spec.passband_edge_hz)
    ax_pass.set_ylim(-2 * spec.passband_ripple_db, 2 * spec.passband_ripple_db)
    _style_axes(ax_pass, f"Frequency (Hz), passband 0 to {spec.passband_edge_hz:g} Hz")
    ax_pass.legend(loc="upper right", fontsize=8, frameon=False, labelcolor=TEXT_SECONDARY)

    fig.savefig(path, dpi=150, metadata={"Software": None})


def summarize(spec: FilterSpec, taps: np.ndarray) -> dict:
    kaiser_numtaps, beta = kaiser_estimate(spec)
    return {
        "spec": asdict(spec),
        "decimation_factor": spec.decimation_factor,
        "cutoff_hz": spec.cutoff_hz,
        "kaiserord_numtaps": kaiser_numtaps,
        "kaiser_beta": beta,
        "edge_samples": edge_samples(taps, spec),
        "measured": measured_response(taps, spec),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    summary = {}
    for fs_in in INPUT_RATES_HZ:
        spec = FilterSpec(fs_in=fs_in)
        taps = design_decimation_filter(spec)
        entry = summarize(spec, taps)
        freqs, gain_db = response_on_grid(taps, spec.fs_in)
        write_csv(args.out_dir / f"response_{fs_in}.csv", freqs, gain_db)
        plot_response(
            args.out_dir / f"response_{fs_in}.png", spec, freqs, gain_db, entry["measured"]
        )
        summary[str(fs_in)] = entry
        print(f"{fs_in} Hz: {entry['measured']}")

    with (args.out_dir / "summary.json").open("w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    print(f"wrote {args.out_dir}")


if __name__ == "__main__":
    main()
