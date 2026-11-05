"""Slide figures for the 2026-11-04 toilet trials: ice drops and stream trials at three sensor
positions.

Run as ``python scripts/plot_toilet_slide_figures.py [data/controlled/raw/<date>]``. Writes
300 dpi PNGs to docs/figures/, styled like plot_floor_slide_figures.py and made for slides:
titles of a few words, no captions (the details are in the data README and the speaker
notes), and no text smaller than MIN_PT, so it stays at 14 pt or more when a figure is shrunk
to about 90% to fit a 13.33 x 7.5 in slide. Each figure comes in three versions, ice drops
only (toilet_ice_*.png), stream trials only (toilet_stream_*.png) and both side by side
(toilet_*.png):

- levels: how loud each ice drop and each stream trial is at each sensor position, in dB
  above that position's own background, against the det-v1 threshold.
- traces: what the detector sees, the second trial of each kind at each position: the 5 ms
  RMS level over time, with the tagged drops or the steady pour.

and toilet_ice_counts.png: the events det-v1 found in each ice trial, the 5 drops and the
other events (second hits, splashes, clicks, background), for the slide about the detector.

A stream trial is a cup of water poured slowly into the bowl from seat height (trial IDs
toilet_<position>_pour_<nn>, excitation water_pour). Drops are the events tagged true_impact
in analysis/<date>/events_<trial>.csv (5 per ice trial); a drop's level is its det-v1 peak
(the 5 ms RMS above 100 Hz). The steady pour is where the detection level, smoothed over
POUR_SMOOTH_S, is more than POUR_RISE_DB above its level at the start of the recording; its
level is the median detection level inside it (less POUR_TRIM_S at each end). Every trial
is measured against the one noise baseline, recorded at the base; the levels figures then
subtract each position's own background: the median, over the position's 6 trials, of the
median detection level in the trial's quiet stretches (after the first SKIP_S, at least
QUIET_GAP_S from any det-v1 event and from the pour), as a noise baseline recorded there
would. The numbers the slides quote are printed.
"""

import csv
import sys
from pathlib import Path

import numpy as np
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from plot_floor_slide_figures import FONT_PT, TEXT, TEXT_2, _style_axes

from everyday_sensing import impacts

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FOLDER = REPO / "data" / "controlled" / "raw" / "2026-11-04"
OUT_DIR = REPO / "docs" / "figures"

POSITIONS = (("base", "#2a78d6", "Base"), ("behindseat", "#eb6834", "Behind seat"),
             ("tanklid", "#1baf7a", "Tank lid"))
"""(trial ID part, colour, label): categorical slots 1 to 3, validated together for
colour-vision deficiency; every chart also names the positions in text."""
OTHER = "#c9c8c3"
"""Events that are not drops, in the counts figure."""
KINDS = ("ice", "stream")
NOISE_ID = "toilet_base_noise_01"
EXAMPLE = 2
"""Trial number shown in the traces figures at every position."""
POUR_SMOOTH_S = 0.5
POUR_RISE_DB = 8.0
POUR_TRIM_S = 0.3
QUIET_GAP_S = 0.5
QUIET_MIN_S = 0.2
"""A trial's quiet stretches count toward its position's background if they add up to this."""
MIN_PT = FONT_PT - 2
"""Smallest text in any figure: 16 pt, so 14 pt or more on the slide."""


def analysis_folder(folder: Path) -> Path:
    return folder.parent.parent / "analysis" / folder.name


def load(folder: Path, trial: str):
    _, x, fs = impacts.load_trial(folder / f"{trial}.json")
    return x.astype(np.float64), fs


def events(folder: Path, trial: str) -> list[dict]:
    with (analysis_folder(folder) / f"events_{trial}.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def drops(folder: Path, trial: str) -> list[tuple[float, float]]:
    """(t_s, peak_db) of the events tagged true_impact."""
    return [(float(r["t_s"]), float(r["peak_db"])) for r in events(folder, trial)
            if r["annotation"] == "true_impact"]


def quiet_level(tr: dict, event_times: list[float], span=None) -> float | None:
    """Median detection level (dB above the baseline) of a trial's quiet stretches, or None
    if they add up to less than QUIET_MIN_S."""
    t = tr["rms_t_s"]
    quiet = t > impacts.SKIP_S
    for e in event_times:
        quiet &= np.abs(t - e) > QUIET_GAP_S
    if span is not None:
        quiet &= (t < span[0] - QUIET_GAP_S) | (t > span[1] + QUIET_GAP_S)
    if quiet.sum() < QUIET_MIN_S * impacts.DETECTION.rate_hz:
        return None
    return float(np.median(tr["level_db"][quiet]))


def pour_span(level_db: np.ndarray, rate: float) -> tuple[float, float]:
    """Start and end (s) of the steady pour in a stream trial's detection level."""
    k = int(POUR_SMOOTH_S * rate)
    smoothed = np.convolve(level_db, np.ones(k) / k, mode="same")
    t = np.arange(level_db.size) / rate
    start_level = np.median(smoothed[(t > 0.1) & (t < 0.6)])
    above = np.flatnonzero(smoothed > start_level + POUR_RISE_DB)
    return above[0] / rate, above[-1] / rate


def collect(folder: Path) -> dict:
    """Everything the figures need, per position: for each kind the levels per trial
    ("db") and the example trace, the event counts of each ice trial, and the position's
    background."""
    _, nx, fs = impacts.load_trial(folder / f"{NOISE_ID}.json")
    baseline = impacts.noise_baseline(nx, fs)
    out = {"baseline": baseline}
    for pos, _, _ in POSITIONS:
        d = out[pos] = {kind: {"db": [], "pour_s": []} for kind in KINDS}
        d["ice"]["counts"] = []
        quiet = []
        for n in (1, 2, 3):
            name = f"toilet_{pos}_ice_{n:02d}"
            x, fs = load(folder, name)
            trial_drops = drops(folder, name)
            if len(trial_drops) != 5:
                raise SystemExit(f"{name}: {len(trial_drops)} drops tagged, not 5")
            d["ice"]["db"].append([db for _, db in trial_drops])
            d["ice"]["counts"].append((len(trial_drops), len(events(folder, name))))
            tr = impacts.detection_traces(x, fs, baseline)
            quiet.append(quiet_level(tr, [float(e["t_s"]) for e in events(folder, name)]))
            if n == EXAMPLE:
                d["ice"]["trace"] = (tr, trial_drops)

            name = f"toilet_{pos}_pour_{n:02d}"
            x, fs = load(folder, name)
            tr = impacts.detection_traces(x, fs, baseline)
            a, b = pour_span(tr["level_db"], impacts.DETECTION.rate_hz)
            quiet.append(quiet_level(tr, [float(e["t_s"]) for e in events(folder, name)],
                                     (a, b)))
            inside = (tr["rms_t_s"] > a + POUR_TRIM_S) & (tr["rms_t_s"] < b - POUR_TRIM_S)
            d["stream"]["db"].append(float(np.median(tr["level_db"][inside])))
            d["stream"]["pour_s"].append(b - a)
            if n == EXAMPLE:
                d["stream"]["trace"] = (tr, (a, b))
        d["quiet_db"] = quiet
        d["own_db"] = float(np.median([q for q in quiet if q is not None]))
    return out


def _median_bar(ax, x: float, value: float, width: float = 0.32) -> None:
    ax.plot([x - width, x + width], [value, value], color=TEXT, linewidth=2.5, zorder=4,
            solid_capstyle="round")
    # A label near the threshold line goes on the far side of the bar from the line.
    gap = value - impacts.DETECTION.threshold_db
    if abs(gap) < 4:
        y, va = (value + 0.8, "bottom") if gap >= 0 else (value - 0.8, "top")
    else:
        y, va = value, "center"
    ax.text(x + width + 0.04, y, f"{value:.0f} dB", va=va, ha="left", fontsize=MIN_PT,
            color=TEXT)


def plot_levels(path: Path, data: dict, kinds: tuple[str, ...]) -> None:
    both = len(kinds) == 2
    fig = Figure(figsize=(12.4, 5.2) if both else (7.6, 5.2), layout="constrained")
    axes = fig.subplots(1, len(kinds), sharey=True, squeeze=False)[0]
    threshold = impacts.DETECTION.threshold_db
    for ax, kind in zip(axes, kinds, strict=True):
        for i, (pos, color, _) in enumerate(POSITIONS):
            own, values = data[pos]["own_db"], data[pos][kind]["db"]
            if kind == "ice":
                for k, trial in enumerate(values):
                    xs = i + (k - 1) * 0.16 + (np.arange(5) - 2) * 0.03
                    ax.plot(xs, np.array(trial) - own, "o", markersize=10, color=color,
                            markeredgecolor="white", markeredgewidth=1.2, zorder=3)
                _median_bar(ax, i, float(np.median(np.concatenate(values))) - own)
            else:
                ax.plot(i + np.array([-0.14, 0.0, 0.14]), np.array(values) - own, "o",
                        markersize=14, color=color, markeredgecolor="white",
                        markeredgewidth=1.5, zorder=3)
                _median_bar(ax, i, float(np.median(values)) - own)
        ax.axhline(threshold, color=TEXT_2, linestyle=(0, (5, 4)), linewidth=1.5, zorder=1)
        ax.set_xticks(range(len(POSITIONS)), [label for _, _, label in POSITIONS])
        ax.set_xlim(-0.55, len(POSITIONS) - 0.2)
        ax.grid(axis="x", visible=False)
        ax.set_title({"ice": "Peak of each drop", "stream": "Median of each pour"}[kind],
                     loc="left")
        _style_axes(ax)
    axes[0].set_ylabel("dB above background")
    axes[0].set_ylim(0, 85 if "ice" in kinds else 32)
    if "ice" in kinds:
        axes[0].text(len(POSITIONS) - 0.22, threshold + 1.0, "threshold", ha="right",
                     va="bottom", color=TEXT_2, fontsize=MIN_PT)
    else:
        axes[0].text(-0.5, threshold - 0.6, "threshold", ha="left", va="top", color=TEXT_2,
                     fontsize=MIN_PT)
    fig.savefig(path, dpi=300, metadata={"Software": None})


def plot_traces(path: Path, data: dict, kinds: tuple[str, ...]) -> None:
    both = len(kinds) == 2
    fig = Figure(figsize=(12.4, 6.6) if both else (12.4, 6.0), layout="constrained")
    axes = fig.subplots(len(POSITIONS), len(kinds), sharey=True, sharex="col", squeeze=False)
    threshold = impacts.DETECTION.threshold_db
    top = 85 if "ice" in kinds else 50
    ticks = [0, 40, 80] if top > 60 else [0, 20, 40]
    for row, (pos, color, label) in zip(axes, POSITIONS, strict=True):
        for ax, kind in zip(row, kinds, strict=True):
            tr, extra = data[pos][kind]["trace"]
            if kind == "ice":
                ax.plot([t for t, _ in extra], [db for _, db in extra], "o", markersize=10,
                        color=color, markeredgecolor="white", markeredgewidth=1.5, zorder=3)
            else:
                ax.axvspan(*extra, color=color, alpha=0.10, linewidth=0)
            ax.plot(tr["rms_t_s"], tr["level_db"], color=color, linewidth=1.0)
            ax.axhline(threshold, color=TEXT_2, linestyle=(0, (5, 4)), linewidth=1.2)
            ax.set_ylim(-10, top)
            ax.set_yticks(ticks, [str(t) for t in ticks[:-1]] + [f"{ticks[-1]} dB"])
            _style_axes(ax)
        row[0].set_ylabel(label, rotation=0, ha="right", va="center", labelpad=12)
    for ax, kind in zip(axes[0], kinds, strict=True):
        ax.set_title({"ice": f"Ice trial {EXAMPLE}", "stream": f"Stream trial {EXAMPLE}"}[kind],
                     loc="left")
    axes[0][-1].text(1.0, threshold + 2, "threshold", transform=axes[0][-1].get_yaxis_transform(),
                     ha="right", va="bottom", color=TEXT_2, fontsize=MIN_PT)
    for ax in axes[-1]:
        ax.set_xlabel("Time (s)")
    fig.savefig(path, dpi=300, metadata={"Software": None})


def plot_counts(path: Path, data: dict) -> None:
    """Events det-v1 found in each ice trial: the 5 tagged drops in the position's colour,
    the other events on top in grey."""
    fig = Figure(figsize=(9.6, 5.2), layout="constrained")
    ax = fig.subplots()
    width = 0.24
    for i, (pos, color, _) in enumerate(POSITIONS):
        for k, (n_drops, n_found) in enumerate(data[pos]["ice"]["counts"]):
            x = i + (k - 1) * (width + 0.04)
            ax.bar(x, n_drops, width, color=color, edgecolor="white", linewidth=1.5)
            ax.bar(x, n_found - n_drops, width, bottom=n_drops, color=OTHER, edgecolor="white",
                   linewidth=1.5)
            ax.text(x, n_found + 0.4, str(n_found), ha="center", va="bottom", fontsize=MIN_PT,
                    color=TEXT)
    ax.axhline(5, color=TEXT_2, linestyle=(0, (5, 4)), linewidth=1.5, zorder=3)
    ax.set_xticks(range(len(POSITIONS)), [label for _, _, label in POSITIONS])
    ax.set_xlim(-0.55, len(POSITIONS) - 0.45)
    most = max(found for pos, _, _ in POSITIONS for _, found in data[pos]["ice"]["counts"])
    ax.set_ylim(0, most * 1.15 + 2)    # room for the count above the tallest bar
    ax.set_ylabel("Events found")
    ax.set_title("Events per ice trial", loc="left")
    ax.grid(axis="x", visible=False)
    _style_axes(ax)
    ax.legend(handles=[Patch(facecolor=OTHER, label="other events"),
                       Line2D([], [], color=TEXT_2, linestyle=(0, (5, 4)), linewidth=1.5,
                              label="5 drops made")],
              loc="upper right", frameon=False, fontsize=MIN_PT)
    fig.savefig(path, dpi=300, metadata={"Software": None})


def report(data: dict) -> None:
    print("dB above the base noise recording (and above the position's own background)")
    for pos, _, label in POSITIONS:
        d = data[pos]
        flat, own = np.concatenate(d["ice"]["db"]), d["own_db"]
        stream = d["stream"]
        print(f"{label:12} own background {own:+.1f} dB (per trial "
              + ", ".join("-" if q is None else f"{q:+.1f}" for q in d["quiet_db"]) + ")")
        print(f"{'':12} ice drops: median {np.median(flat):5.1f} ({np.median(flat) - own:5.1f}) "
              f"dB, range {flat.min():5.1f} to {flat.max():5.1f}; events found per trial "
              + ", ".join(str(found) for _, found in d["ice"]["counts"]))
        print(f"{'':12} stream trials: " + ", ".join(f"{v:4.1f} ({v - own:4.1f})"
                                                    for v in stream["db"])
              + " dB over " + ", ".join(f"{s:3.1f}" for s in stream["pour_s"]) + " s")


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    folder = Path(argv[0]) if argv else DEFAULT_FOLDER
    data = collect(folder)
    report(data)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for prefix, kinds in (("toilet", KINDS), ("toilet_ice", ("ice",)),
                          ("toilet_stream", ("stream",))):
        for name, plot in (("levels", plot_levels), ("traces", plot_traces)):
            path = OUT_DIR / f"{prefix}_{name}.png"
            plot(path, data, kinds)
            print(path)
    path = OUT_DIR / "toilet_ice_counts.png"
    plot_counts(path, data)
    print(path)


if __name__ == "__main__":
    main()
