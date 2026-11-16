"""Figures of the trade show poster (2026-11-17), with text large enough for a 32 x 40 in print.

    python scripts/plot_poster_figures.py

Writes 300 dpi PNGs to docs/figures/ (poster_*.png), each sized in inches to the box it fills on the
poster's 30 x 37.5 unit layout (printed 1.067 times larger), so FONT_PT is about 28 pt in print:

- poster_floor_drop_distance.png: peak det-v1 level of the 4 in drop at 1, 2 and 3 m on carpet and
  wood, bare against enclosed (2026-10-21): mean of the trials, whiskers their range.
- poster_floor_detection_range.png: share of steps at or above the det-v1 threshold at 1, 2 and 3 m
  (points; whiskers the range over trials) and the fit of scripts/step_detection_range.py
  (curves; past 3 m, where nothing was walked, they are drawn faint on a grey band).
- poster_floor_spectrograms.png: spectrogram of the 4 in drop at 1 m, averaged over the trials of
  each floor and condition, from 10 ms before the loudest point to 100 ms after, on one
  colour scale relative to the loudest point of the four panels.
- poster_toilet_levels.png: ice drops and water pours at each sensor position (2026-11-04), as
  scripts/plot_toilet_slide_figures.py measures them: the median, with whiskers for the
  middle half of the 15 drops and for the range of the 3 pours.
- poster_toilet_model.png: the ice-against-pour classifier of scripts/toilet_ice_vs_pour.py in three
  steps: the detection level of an ice trial and a stream trial at the base, every labelled
  2026-11-04 event by how far it rises above the level just before it and by its peak level, and
  the labels the model gives the 2026-11-11 events.

Colour carries one meaning throughout: orange is carpet or ice, blue is wood or water; a
filled marker and a solid line are the bare sensor, a hollow marker and a dashed line the
enclosed one.
"""

import csv
import functools
from pathlib import Path

import matplotlib as mpl
import numpy as np
import plot_toilet_slide_figures as toilet
import step_detection_range as steps
import toilet_ice_vs_pour as model
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from scipy import signal

from everyday_sensing import impacts

REPO = Path(__file__).resolve().parents[1]
CONTROLLED = REPO / "data" / "controlled"
FLOOR = "2026-10-21"
OUT_DIR = REPO / "docs" / "figures"
FONT_PT = 26
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, MUTED, GRID, GREY, EMPTY = "#1f1f1e", "#55544f", "#deddd8", "#7a7972", "#ecebe7"
THRESHOLD_DB = impacts.DETECTION.threshold_db
RC = {
    "font.family": ["Arial", "Liberation Sans", "DejaVu Sans"], "font.size": FONT_PT,
    "axes.edgecolor": MUTED, "axes.labelcolor": INK, "axes.labelsize": FONT_PT,
    "axes.titlesize": FONT_PT, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.linewidth": 1.6, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": False, "xtick.color": INK, "ytick.color": INK,
    "xtick.labelsize": FONT_PT - 2, "ytick.labelsize": FONT_PT - 2,
    "xtick.major.width": 1.6, "ytick.major.width": 1.6, "xtick.major.size": 6,
    "ytick.major.size": 6, "legend.frameon": False, "legend.fontsize": FONT_PT - 3,
}
MOUNT_HANDLES = [
    Line2D([], [], color=GREY, marker="o", ms=13, lw=3, mfc=GREY, label="Bare"),
    Line2D([], [], color=GREY, marker="o", ms=13, lw=3, ls="--", mfc="white", mew=2.5,
           label="Enclosed"),
]
FLOORS = (("carpet", ORANGE), ("wood", BLUE))
MOUNTS = (("bare", "-"), ("enclosed", "--"))


def poster_style(plot):
    """Run a plot function with the poster's rcParams, whatever the imports above set."""
    @functools.wraps(plot)
    def styled(*args, **kwargs):
        with mpl.rc_context(RC):
            return plot(*args, **kwargs)
    return styled


def _save(fig: Figure, name: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    fig.savefig(path, dpi=300, facecolor="white", metadata={"Software": None})
    return path


def _grid(ax, axis: str = "y") -> None:
    ax.grid(axis=axis, color=GRID, linewidth=1.2)
    ax.set_axisbelow(True)


def drop_levels() -> dict:
    """{(floor, condition, radius in m): [peak det-v1 level of each 4 in drop trial, dB]}."""
    out = {}
    for path in sorted((CONTROLLED / "analysis" / FLOOR).glob("events_*drop4in*.csv")):
        floor, condition, _, radius, _ = path.stem.removeprefix("events_").split("_")
        with path.open(newline="") as f:
            peak = max(float(e["peak_db"]) for e in csv.DictReader(f))
        out.setdefault((floor, condition, int(radius[0])), []).append(peak)
    return out


@poster_style
def plot_drop_distance() -> Path:
    levels = drop_levels()
    fig = Figure(figsize=(8.92, 4.6))
    axes = fig.subplots(1, 2, sharey=True)
    for ax, (floor, colour) in zip(axes, FLOORS, strict=True):
        for (condition, line), dx in zip(MOUNTS, (-0.06, 0.06), strict=True):
            trials = [np.array(levels[(floor, condition, r)]) for r in (1, 2, 3)]
            mean = np.array([t.mean() for t in trials])
            low, high = np.array([t.min() for t in trials]), np.array([t.max() for t in trials])
            ax.errorbar(np.array([1, 2, 3]) + dx, mean, yerr=[mean - low, high - mean], fmt="o",
                        ls=line, color=colour, mfc=colour if condition == "bare" else "white",
                        mew=2.8, ms=15, lw=3.2)
        ax.axhline(THRESHOLD_DB, color=MUTED, ls=(0, (5, 4)), lw=2)
        ax.set(title=floor.capitalize(), xticks=[1, 2, 3], xlim=(0.55, 3.45), ylim=(0, 82),
               yticks=[0, 20, 40, 60, 80], xlabel="Distance (m)")
        _grid(ax)
    axes[0].set_ylabel("Peak level (dB)")
    axes[0].legend(handles=MOUNT_HANDLES, loc="upper right", bbox_to_anchor=(1.06, 1.04),
                   handlelength=1.6, labelspacing=0.25, borderpad=0)
    axes[1].text(3.42, 11, "threshold", ha="right", va="top", color=MUTED, fontsize=FONT_PT - 6)
    fig.tight_layout(w_pad=1.0)
    return _save(fig, "poster_floor_drop_distance.png")


@poster_style
def plot_detection_range() -> Path:
    trials = steps.step_levels()
    fig = Figure(figsize=(8.92, 4.6))
    ax = fig.subplots()
    ax.axvspan(3, 5, color="#efeeea", lw=0)
    ax.text(4.0, 80, "not tested", ha="center", va="center", color=MUTED, fontsize=FONT_PT - 4)
    r = np.linspace(0.8, 5, 300)
    for floor, colour in FLOORS[::-1]:
        for condition, line in MOUNTS:
            group = steps.group(trials, floor, condition)
            share = 100 * steps.share_detected(steps.fit(group), r)
            ax.plot(r[r <= 3], share[r <= 3], line, color=colour, lw=3.4)
            ax.plot(r[r >= 3], share[r >= 3], line, color=colour, lw=3.4, alpha=0.45)
            seen = [steps.measured(group, radius) for radius in steps.RADII]
            y = np.array([100 * m["share"] for m in seen])
            low, high = (np.array([100 * m[k] for m in seen]) for k in ("low", "high"))
            dx = (-0.05 if floor == "wood" else 0.05) + (-0.025 if condition == "bare" else 0.025)
            ax.errorbar(np.array(steps.RADII) + dx, y, yerr=[y - low, high - y], fmt="o", ms=15,
                        color=colour, mfc=colour if condition == "bare" else "white", mew=2.8,
                        lw=2.4)
    ax.set(xlim=(0.8, 5), ylim=(0, 108), xticks=[1, 2, 3, 4, 5], yticks=[0, 50, 100],
           xlabel="Distance (m)", ylabel="Steps detected (%)")
    _grid(ax)
    handles = [Line2D([], [], color=BLUE, lw=5, label="Wood"),
               Line2D([], [], color=ORANGE, lw=5, label="Carpet"), *MOUNT_HANDLES]
    ax.legend(handles=handles, loc="lower left", ncol=2, columnspacing=1.2, handlelength=1.6,
              labelspacing=0.25, borderaxespad=0.3)
    fig.tight_layout()
    return _save(fig, "poster_floor_detection_range.png")


def mean_spectrogram(floor: str, condition: str):
    """(frequency in Hz, time from the loudest point in s, mean power) of the 1 m drops."""
    powers = []
    for path in sorted((CONTROLLED / "raw" / FLOOR).glob(f"{floor}_{condition}_drop4in_1m_*.json")):
        _, x, fs = impacts.load_trial(path)
        traces = impacts.detection_traces(x, fs)
        start = int((traces["rms_t_s"][np.argmax(traces["level_db"])] - 0.02) * fs)
        segment = x[start:start + int(0.14 * fs)].astype(np.float64)
        f, t, p = signal.spectrogram(segment - segment.mean(), fs, nperseg=384, noverlap=336,
                                     nfft=1024, scaling="density")
        powers.append(p)
    return f, t - 0.02, np.mean(powers, axis=0)


@poster_style
def plot_spectrograms() -> Path:
    spectra = {(floor, condition): mean_spectrogram(floor, condition)
               for floor, _ in FLOORS for condition, _ in MOUNTS}
    loudest = max(p[(f >= 100) & (f <= 8000)].max() for f, _, p in spectra.values())
    cmap = LinearSegmentedColormap.from_list(
        "blue", ["#ffffff", "#cde2fb", "#86b6ef", "#2a78d6", "#184f95", "#0d366b"])
    fig = Figure(figsize=(11.4, 4.2))
    grid = fig.add_gridspec(1, 5, width_ratios=[1, 1, 1, 1, 0.09], wspace=0.22, left=0.085,
                            right=0.925, top=0.76, bottom=0.27)
    for k, ((floor, condition), (f, t, p)) in enumerate(spectra.items()):
        ax = fig.add_subplot(grid[0, k])
        keep = f <= 8000
        mesh = ax.pcolormesh(1000 * t, f[keep] / 1000, 10 * np.log10(p[keep] / loudest + 1e-12),
                             vmin=-60, vmax=0, cmap=cmap, shading="gouraud", rasterized=True)
        ax.set_title(f"{floor.capitalize()}\n{condition}", fontsize=FONT_PT - 3, linespacing=0.95)
        ax.set(xlim=(-10, 100), yticks=[0, 4, 8])
        ax.set_xticks([0, 50, 100], ["0", "", "100"])
        if k:
            ax.set_yticklabels([])
        else:
            ax.set_ylabel("Freq. (kHz)", fontsize=FONT_PT - 2)
    fig.text(0.47, 0.03, "Time after impact (ms)", ha="center", va="bottom", fontsize=FONT_PT - 2)
    cax = fig.add_subplot(grid[0, 4])
    fig.colorbar(mesh, cax=cax, ticks=[-60, -30, 0]).outline.set_visible(False)
    cax.set_title("dB", fontsize=FONT_PT - 2, fontweight="normal", loc="left", pad=10)
    return _save(fig, "poster_floor_spectrograms.png")


@poster_style
def plot_toilet_levels(data: dict) -> Path:
    fig = Figure(figsize=(6.3, 5.0))
    axes = fig.subplots(1, 2, sharey=True)
    for ax, kind, colour, marker, title in ((axes[0], "ice", ORANGE, "D", "Ice"),
                                            (axes[1], "stream", BLUE, "o", "Water")):
        for i, (position, _, _) in enumerate(toilet.POSITIONS):
            values = np.ravel(data[position][kind]["db"]) - data[position]["own_db"]
            median = float(np.median(values))
            low, high = (np.percentile(values, [25, 75]) if kind == "ice"
                         else (values.min(), values.max()))
            ax.errorbar([i], [median], yerr=[[median - low], [high - median]], fmt=marker, ms=17,
                        color=colour, mec="white", mew=1.5, lw=4, capsize=9, capthick=4)
            ax.text(i, high + 5, f"{median:.0f}", ha="center", va="bottom",
                    fontsize=FONT_PT - 1, color=INK, fontweight="bold")
        ax.axhline(THRESHOLD_DB, color=MUTED, ls=(0, (5, 4)), lw=2)
        ax.set(title=title, xlim=(-0.6, 2.6))
        ax.set_xticks(range(3), ["Base", "Seat", "Lid"], fontsize=FONT_PT - 6)
        _grid(ax)
    axes[0].set(ylim=(0, 85), yticks=[0, 20, 40, 60, 80], ylabel="Level (dB)")
    axes[1].text(2.55, 11, "threshold", ha="right", va="top", color=MUTED, fontsize=FONT_PT - 7)
    fig.tight_layout(w_pad=0.6)
    return _save(fig, "poster_toilet_levels.png")


@poster_style
def plot_toilet_model(data: dict) -> Path:
    train, test = model.collect(*model.TRAIN), model.collect(*model.TEST)
    counts = model.confusion(test, model.test_on(train, test))["counts"]
    fig = Figure(figsize=(11.54, 5.0))
    grid = fig.add_gridspec(2, 3, width_ratios=[1.3, 1.15, 0.95], wspace=0.62, hspace=0.22,
                            left=0.1, right=0.985, top=0.9, bottom=0.2)
    # 1. The detection level of the example ice trial and stream trial at the base.
    for row, (kind, colour, label) in enumerate((("ice", ORANGE, "Ice drops"),
                                                 ("stream", BLUE, "Water pour"))):
        traces = data["base"][kind]["trace"][0]
        level = np.maximum(traces["level_db"], 0)
        every = 20                                   # the highest of each 20 ms, to keep spikes
        tops = level[:level.size // every * every].reshape(-1, every).max(axis=1)
        ax = fig.add_subplot(grid[row, 0])
        ax.fill_between(traces["rms_t_s"][::every][:tops.size], 0, tops, color=colour, lw=0,
                        alpha=0.9)
        ax.set(xlim=(0, 12), ylim=(0, 85), yticks=[0, 40, 80], xticks=[0, 4, 8, 12])
        ax.text(0.985, 0.93, label, transform=ax.transAxes, ha="right", va="top",
                fontsize=FONT_PT - 2, fontweight="bold", color=INK)
        _grid(ax)
        if row == 0:
            ax.set_xticklabels([])
            ax.set_title("1. The signal")
        else:
            ax.set_xlabel("Time (s)")
    fig.text(0.018, 0.56, "Level (dB)", rotation=90, va="center", ha="center", fontsize=FONT_PT)
    # 2. Every labelled training event.
    ax = fig.add_subplot(grid[:, 1])
    for kind, colour, label, marker, size in (("pour", BLUE, "Pour", "o", 110),
                                              ("ice", ORANGE, "Ice", "D", 120)):
        rows = [r for r in train if r["kind"] == kind]
        ax.scatter([r["rise_db"] for r in rows], [r["peak_db"] for r in rows], s=size,
                   marker=marker, color=colour, edgecolor="white", linewidth=1.1, label=label,
                   alpha=0.95 if kind == "ice" else 0.7, zorder=3 if kind == "ice" else 2)
    ax.set(title="2. Each event", xlabel="Jump from quiet (dB)", xticks=[0, 40, 80],
           yticks=[20, 40, 60, 80], xlim=(-8, 86), ylim=(8, 86))
    ax.set_ylabel("Peak level (dB)", labelpad=8)
    _grid(ax, "both")
    ax.legend(loc="upper left", handletextpad=0.0, borderaxespad=0.1, labelspacing=0.2,
              markerscale=1.3)
    # 3. What the model says about the session it has not seen.
    ax = fig.add_subplot(grid[:, 2])
    ax.set_title("3. The test")
    for r, actual in enumerate(("ice", "pour")):
        for c, said in enumerate(("ice", "pour")):
            n = counts[(actual, said)]
            colour = (ORANGE if actual == "ice" else BLUE) if actual == said else EMPTY
            ax.add_patch(Rectangle((c + 0.04, 1 - r + 0.04), 0.92, 0.92, color=colour, lw=0))
            ax.text(c + 0.5, 1 - r + 0.5, str(n), ha="center", va="center",
                    fontsize=FONT_PT + 16, fontweight="bold",
                    color="white" if actual == said else GREY)
    ax.set(xlim=(0, 2), ylim=(0, 2), xlabel="Model says")
    ax.set_aspect("equal", adjustable="box", anchor="N")
    ax.set_xticks([0.5, 1.5], ["Ice", "Pour"])
    ax.set_yticks([1.5, 0.5], ["Ice", "Pour"], rotation=90, va="center")
    ax.set_ylabel("Actually", labelpad=4)
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    correct, total = counts[("ice", "ice")] + counts[("pour", "pour")], len(test)
    ax.text(1.0, -0.82, f"{correct} / {total}", ha="center", va="top", fontsize=FONT_PT + 10,
            fontweight="bold", color=INK, clip_on=False)
    ax.text(1.0, -1.42, "correct", ha="center", va="top", color=INK, clip_on=False)
    return _save(fig, "poster_toilet_model.png")


def main() -> None:
    data = toilet.collect(toilet.DEFAULT_FOLDER)
    for path in (plot_drop_distance(), plot_detection_range(), plot_spectrograms(),
                 plot_toilet_levels(data), plot_toilet_model(data)):
        print(path)


if __name__ == "__main__":
    main()
