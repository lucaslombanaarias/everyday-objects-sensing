"""Slide figures for the 2026-10-21 walking trials, bare sensor against enclosed.

Run as ``python scripts/plot_floor_slide_figures.py [data/controlled/raw/<date>]``. Writes
two 300 dpi PNGs to docs/figures/, sized in inches to the box they fill on a 13.33 x 7.5 in
slide, so their text is FONT_PT on the slide when inserted at 100%:

- steps_counted_walking.png: the steps in the signal of each walking trial (10 made per
  trial), one dot per trial, by floor, radius and condition.
- wood_walking_spectra.png: the average spectrum of the steps on wood, bare against
  enclosed, at each radius, with the noise baseline.

Both come from the manual review in analysis/<date>/walking_steps_review.csv. They are the
presentation versions of floor_detection.png (with --walking) and floor_spectra_wood.png
from scripts/plot_floor_slides.py: step counts per trial instead of a share per condition,
and spectra of a 100 ms window on each reviewed step. The steps of a trial are its det-v1
events, less the ones the review lists as not steps, plus the ones it lists as missed; a
trial the review leaves undecided is left out. The band differences the slide quotes are
printed, with det-v1's count next to the reviewed count for each trial.
"""

import csv
import re
import sys
from pathlib import Path

import matplotlib as mpl
import numpy as np
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedLocator, NullFormatter

from everyday_sensing import impacts
from everyday_sensing.filter_report import RESONANCE_BAND_HZ

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FOLDER = REPO / "data" / "controlled" / "raw" / "2026-10-21"
OUT_DIR = REPO / "docs" / "figures"

FONT_PT = 18
SMALL_PT = 12
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
MUTED = "#8a8985"
GRID = "#e4e3df"
BARE = "#2a78d6"
ENCLOSED = "#eb6834"
NOISE = "#6f6e6a"
BAND = "#8a8985"
CONDITIONS = (("bare", BARE, "bare sensor"), ("enclosed", ENCLOSED, "enclosed sensor"))
FLOORS = ("wood", "carpet")
RADII_M = (1.0, 2.0, 3.0)

SEGMENT_S = 0.1
"""Length of the Hann window centered on each step for its spectrum."""
SMOOTH_OCTAVE = 1 / 6
"""Width of the fractional-octave smoothing of the plotted spectra."""
MATCH_S = 0.05
"""A det-v1 event this close to a time the review lists as not a step is that event (the
review gives times to 0.01 s)."""
F_MIN_HZ, F_MAX_HZ = 100.0, 20000.0
HIGH_BAND_HZ = (2000.0, 8000.0)
LOW_BAND_HZ = (500.0, 2000.0)

mpl.rcParams.update(
    {
        "font.family": "Trebuchet MS",
        "font.size": FONT_PT,
        "axes.labelsize": FONT_PT + 2,
        "axes.titlesize": FONT_PT + 2,
        "xtick.labelsize": FONT_PT,
        "ytick.labelsize": FONT_PT,
        "legend.fontsize": FONT_PT,
        "axes.labelcolor": TEXT,
        "axes.titlecolor": TEXT,
        "xtick.color": TEXT_2,
        "ytick.color": TEXT_2,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
    }
)


def _style_axes(ax) -> None:
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)


def review_rows(folder: Path) -> list[dict]:
    """Rows of the manual review of the folder's walking trials."""
    path = folder.parent.parent / "analysis" / folder.name / "walking_steps_review.csv"
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _times(field: str) -> list[float]:
    """The times in a review field such as '4.65 s 14.0 dB; 5.06 s spike'."""
    return [float(t) for t in re.findall(r"([\d.]+) s\b", field)]


def walking_trials(folder: Path, review: list[dict]) -> dict:
    """{(floor, condition, radius): [trial dict, ...]} for the reviewed walking trials, in
    recording order. Each trial dict is analyze_folder's, plus "steps": the times of the
    steps the review counts (det-v1 events less the review's not-steps, plus its missed
    steps)."""
    by_id = {r["trial_id"]: r for r in review}
    out = {}
    for t in impacts.analyze_folder(folder):
        meta = t["meta"]
        r = by_id.get(meta["trial_id"])
        if t["result"] is None or r is None or not r["steps_in_signal"]:
            continue
        not_steps = _times(r["not_steps_at"])
        steps = [e["t_s"] for e in t["result"]["impacts"]
                 if all(abs(e["t_s"] - n) > MATCH_S for n in not_steps)]
        steps = sorted(steps + _times(r["missed_at"]))
        if len(steps) != int(r["steps_in_signal"]):
            raise SystemExit(f"{meta['trial_id']}: {len(steps)} step times from the review's "
                             f"events, but {r['steps_in_signal']} steps in the signal")
        t["steps"] = steps
        out.setdefault((r["floor"], r["condition"], float(r["radius_m"])), []).append(t)
    return out


def plot_counts(path: Path, review: list[dict]) -> None:
    made = {int(r["steps_planned"]) for r in review}
    if len(made) != 1:
        raise SystemExit(f"walking trials with different steps made: {sorted(made)}")
    made = made.pop()
    left_out = [r for r in review if not r["steps_in_signal"]]
    trials = {}
    for r in review:
        if r["steps_in_signal"]:
            key = (r["floor"], r["condition"], float(r["radius_m"]))
            trials.setdefault(key, []).append(int(r["steps_in_signal"]))
    found = [n for group in trials.values() for n in group]
    y_lo, y_hi = min(min(found), made) - 1.5, max(max(found), made) + 1.5

    fig = Figure(figsize=(11.0, 5.3), layout="constrained")
    axes = fig.subplots(1, len(FLOORS), sharey=True)
    offsets = (-0.12, 0.0, 0.12)
    for ax, floor in zip(axes, FLOORS):
        ax.axhline(made, color=TEXT_2, linestyle=(0, (5, 4)), linewidth=1.5, zorder=1)
        for i, radius in enumerate(RADII_M):
            for (condition, color, _), side in zip(CONDITIONS, (-0.2, 0.2)):
                group = trials.get((floor, condition, radius), [])
                for n, dx in zip(group, offsets):
                    ax.plot(i + side + dx, n, "o", markersize=14, color=color,
                            markeredgecolor="white", markeredgewidth=1.5, zorder=3)
        if not any(trials.get((floor, "enclosed", r)) for r in RADII_M):
            ax.text(1, y_lo + 0.5, "no enclosed walking trials", ha="center", va="center",
                    color=MUTED, fontsize=FONT_PT - 2)
        ax.set_title(floor.capitalize(), loc="left")
        ax.set_xticks(range(len(RADII_M)), [f"{r:g} m" for r in RADII_M])
        ax.set_xlim(-0.6, len(RADII_M) - 0.4)
        ax.set_ylim(y_lo, y_hi)
        ax.yaxis.set_major_locator(FixedLocator(np.arange(np.ceil(y_lo), y_hi)))
        ax.grid(axis="x", visible=False)
        _style_axes(ax)
    axes[0].set_ylabel("steps counted per trial")
    axes[-1].text(len(RADII_M) - 0.42, made + 0.15, f"{made} steps taken", ha="right",
                  va="bottom", color=TEXT_2, fontsize=FONT_PT - 2)
    handles = [Line2D([], [], marker="o", linestyle="", markersize=14, color=color,
                      markeredgecolor="white", label=label) for _, color, label in CONDITIONS]
    axes[-1].legend(handles=handles, loc="upper right", frameon=False)
    skipped = "; ".join(
        f"{r['floor']}, {r['condition']}, {float(r['radius_m']):g} m: one trial left out "
        "(irregular timing)" for r in left_out)
    fig.text(0.0, 0.0, f"2026-10-21: one dot per trial, {made} steps in place per trial, 3 "
             "trials per condition. Steps counted by hand from the signal and the step "
             "rhythm,\nincluding steps below the detection threshold"
             + (f"; {skipped}" if skipped else "") + ". Distance: feet to sensor.",
             fontsize=SMALL_PT, color=MUTED, ha="left", va="bottom")
    fig.get_layout_engine().set(rect=(0, 0.08, 1, 0.92))
    fig.savefig(path, dpi=300, metadata={"Software": None})


def _psd(segment: np.ndarray, fs: float) -> np.ndarray:
    """One-sided Hann-windowed power spectral density of a segment, in counts^2/Hz."""
    w = np.hanning(segment.size)
    spectrum = np.fft.rfft((segment - segment.mean()) * w)
    return 2 * np.abs(spectrum) ** 2 / (fs * np.sum(w**2))


def step_spectrum(group: list[dict], folder: Path) -> tuple[np.ndarray, np.ndarray, int]:
    """Frequencies, mean PSD of every step in the group's trials (a SEGMENT_S window
    centered on each step) and the number of steps averaged."""
    psds, fs = [], None
    for t in group:
        _, x, fs = impacts.load_trial(folder / f"{t['meta']['trial_id']}.json")
        n = int(round(SEGMENT_S * fs))
        for when in t["steps"]:
            start = int(round(when * fs)) - n // 2
            if start >= 0 and start + n <= x.size:
                psds.append(_psd(x[start : start + n].astype(np.float64), fs))
    n = int(round(SEGMENT_S * fs))
    return np.fft.rfftfreq(n, d=1 / fs), np.mean(psds, axis=0), len(psds)


def noise_spectrum(trial_id: str, folder: Path) -> tuple[np.ndarray, np.ndarray]:
    """Mean PSD of consecutive SEGMENT_S windows of a noise-only trial after its first
    SKIP_S (the same start-up skip as the det-v1 noise baseline)."""
    _, x, fs = impacts.load_trial(folder / f"{trial_id}.json")
    n = int(round(SEGMENT_S * fs))
    x = x[int(impacts.SKIP_S * fs) :].astype(np.float64)
    psds = [_psd(x[i : i + n], fs) for i in range(0, x.size - n + 1, n)]
    return np.fft.rfftfreq(n, d=1 / fs), np.mean(psds, axis=0)


def smooth(freqs: np.ndarray, psd: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Mean PSD over SMOOTH_OCTAVE around each grid frequency."""
    half = 2 ** (SMOOTH_OCTAVE / 2)
    return np.array([psd[(freqs >= f / half) & (freqs <= f * half)].mean() for f in grid])


def band_db(freqs: np.ndarray, psd: np.ndarray, band: tuple[float, float]) -> float:
    """Power in a band, in dB re 1 count^2."""
    keep = (freqs >= band[0]) & (freqs < band[1])
    return 10 * np.log10(np.sum(psd[keep]) * (freqs[1] - freqs[0]))


def plot_spectra(path: Path, trials: dict, folder: Path) -> None:
    grid = np.geomspace(F_MIN_HZ, F_MAX_HZ, 500)
    noise_id = trials[("wood", "bare", RADII_M[0])][0]["result"]["baseline_source"]
    nf, npsd = noise_spectrum(noise_id, folder)
    noise_db = 10 * np.log10(smooth(nf, npsd, grid))

    fig = Figure(figsize=(12.4, 4.3), layout="constrained")
    axes = fig.subplots(1, len(RADII_M), sharey=True)
    counts, curves = [], []
    for ax, radius in zip(axes, RADII_M):
        ax.axvspan(*RESONANCE_BAND_HZ, color=BAND, alpha=0.12, linewidth=0)
        ax.plot(grid, noise_db, color=NOISE, linestyle=(0, (3, 2)), linewidth=2)
        spectra = {}
        for condition, color, _ in CONDITIONS:
            f, psd, n = step_spectrum(trials[("wood", condition, radius)], folder)
            spectra[condition] = (f, psd)
            counts.append(n)
            ax.plot(grid, 10 * np.log10(smooth(f, psd, grid)), color=color, linewidth=2.6)
        curves.append((radius, spectra))
        ax.set_xscale("log")
        ax.set_xlim(F_MIN_HZ, F_MAX_HZ)
        ax.xaxis.set_major_locator(FixedLocator([100, 1000, 10000]))
        ax.set_xticklabels(["100 Hz", "1 kHz", "10 kHz"])
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.set_title(f"{radius:g} m from the sensor", loc="left")
        _style_axes(ax)
    axes[0].set_ylabel("power (dB)")
    axes[1].set_xlabel("frequency")
    lo, hi = axes[0].get_ylim()
    axes[0].text(sum(RESONANCE_BAND_HZ) / 2, lo + 0.04 * (hi - lo), "sensor\nresonance",
                 ha="center", va="bottom", color=TEXT_2, fontsize=SMALL_PT + 2,
                 linespacing=1.0)
    handles = [Line2D([], [], color=color, linewidth=2.6, label=label)
               for _, color, label in CONDITIONS]
    handles.append(Line2D([], [], color=NOISE, linewidth=2, linestyle=(0, (3, 2)),
                          label="noise baseline"))
    fig.legend(handles=handles, loc="outside upper right", ncol=3, frameon=False)
    fig.text(0.0, 0.0, f"Each curve: the average spectrum of the {min(counts)} to "
             f"{max(counts)} steps on wood from the manual review, {SEGMENT_S * 1000:g} ms "
             "Hann window per step, unfiltered.\n1/6-octave smoothed; dB re 1 count²/Hz. "
             "Shaded: the sensor's 4 to 5 kHz resonance (datasheet). Noise baseline: "
             f"{noise_id}.", fontsize=SMALL_PT, color=MUTED, ha="left", va="bottom")
    fig.get_layout_engine().set(rect=(0, 0.1, 1, 0.9), wspace=0.06)
    fig.savefig(path, dpi=300, metadata={"Software": None})

    for (radius, spectra), (n_bare, n_enc) in zip(curves, zip(counts[::2], counts[1::2])):
        diffs = {name: band_db(*spectra["enclosed"], band) - band_db(*spectra["bare"], band)
                 for name, band in (("2-8 kHz", HIGH_BAND_HZ), ("0.5-2 kHz", LOW_BAND_HZ),
                                    ("all, 100 Hz-20 kHz", (F_MIN_HZ, F_MAX_HZ)))}
        print(f"wood {radius:g} m ({n_bare} bare, {n_enc} enclosed steps), enclosed minus "
              "bare: " + ", ".join(f"{name} {d:+.1f} dB" for name, d in diffs.items()))


def print_counts(trials: dict, review: list[dict]) -> None:
    by_key = {}
    for r in review:
        by_key.setdefault((r["floor"], r["condition"], float(r["radius_m"])), []).append(r)
    for key, rows in sorted(by_key.items()):
        print(f"{key[0]} {key[1]} {key[2]:g} m: det-v1 {[int(r['detv1_found']) for r in rows]}, "
              f"in the signal {[r['steps_in_signal'] or 'left out' for r in rows]}")
    counted = [r for r in review if r["steps_in_signal"]]
    exact = [r for r in counted if r["steps_in_signal"] == r["steps_planned"]]
    same = [r for r in counted if r["steps_in_signal"] == r["detv1_found"]]
    enclosed = [r for r in counted if r["condition"] == "enclosed"]
    print(f"{len(exact)} of {len(counted)} counted trials have {review[0]['steps_planned']} "
          f"steps in the signal; det-v1 matches the review in {len(same)} of {len(counted)}, "
          f"{sum(r in same for r in enclosed)} of {len(enclosed)} enclosed")


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    folder = Path(argv[0]) if argv else DEFAULT_FOLDER
    review = review_rows(folder)
    trials = walking_trials(folder, review)
    print_counts(trials, review)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    plot_counts(OUT_DIR / "steps_counted_walking.png", review)
    plot_spectra(OUT_DIR / "wood_walking_spectra.png", trials, folder)
    print(f"wrote steps_counted_walking.png and wood_walking_spectra.png to {OUT_DIR}")


if __name__ == "__main__":
    main()
