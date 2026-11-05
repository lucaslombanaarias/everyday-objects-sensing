"""Slide figures for the 2026-11-04 toilet trials: ice drops and stream trials at three sensor
positions.

Run as ``python scripts/plot_toilet_slide_figures.py [data/controlled/raw/<date>]``. Writes
300 dpi PNGs to docs/figures/, sized and styled like plot_floor_slide_figures.py (text is
FONT_PT on a 13.33 x 7.5 in slide when inserted at 100%), each in three versions: ice drops
only (toilet_ice_*.png), stream trials only (toilet_stream_*.png), and both side by side
(toilet_*.png):

- levels: how loud each ice drop and each stream trial is at each sensor position, in dB
  above that position's own background, against the det-v1 threshold.
- traces: what the detector sees, the second trial of each kind at each position: the 5 ms
  RMS level over time, with the tagged drops or the steady pour.
- spectra: the average spectrum of the drops or of the pours at each position, with the
  noise baseline.

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
import textwrap
from pathlib import Path

import numpy as np
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from plot_floor_slide_figures import (
    BAND,
    FONT_PT,
    MUTED,
    NOISE,
    SEGMENT_S,
    SMALL_PT,
    TEXT,
    TEXT_2,
    _psd,
    _style_axes,
    smooth,
)

from everyday_sensing import impacts
from everyday_sensing.filter_report import RESONANCE_BAND_HZ

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FOLDER = REPO / "data" / "controlled" / "raw" / "2026-11-04"
OUT_DIR = REPO / "docs" / "figures"

POSITIONS = (("base", "#2a78d6", "Base"), ("behindseat", "#eb6834", "Behind seat"),
             ("tanklid", "#1baf7a", "Tank lid"))
"""(trial ID part, colour, label): categorical slots 1 to 3, validated together for
colour-vision deficiency; every chart also names the positions in text."""
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
PRE_S = 0.010
"""A drop's spectrum is a SEGMENT_S Hann window starting this long before its event time."""
F_MIN_HZ, F_MAX_HZ = 100.0, 10000.0
"""Spectra stop at 10 kHz, the top of the wavelet figures; above it every trial is at the
electronics floor."""
CAPTION_CHARS_PER_IN = 13
"""Characters of caption text per inch of figure width (SMALL_PT Trebuchet MS)."""
SETUP = {
    "ice": "3 ice trials per position, 5 ice cubes each, dropped from seat height into the "
           "bowl",
    "stream": "3 stream trials per position, a cup of water poured slowly from seat height "
              "into the bowl",
}


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
    ("db"), the spectra ("psd") and the example trace, and the position's background."""
    _, nx, fs = impacts.load_trial(folder / f"{NOISE_ID}.json")
    baseline = impacts.noise_baseline(nx, fs)
    out = {"baseline": baseline}
    for pos, _, _ in POSITIONS:
        d = out[pos] = {kind: {"db": [], "psd": [], "pour_s": []} for kind in KINDS}
        quiet = []
        for n in (1, 2, 3):
            name = f"toilet_{pos}_ice_{n:02d}"
            x, fs = load(folder, name)
            m = int(round(SEGMENT_S * fs))
            trial_drops = drops(folder, name)
            if len(trial_drops) != 5:
                raise SystemExit(f"{name}: {len(trial_drops)} drops tagged, not 5")
            d["ice"]["db"].append([db for _, db in trial_drops])
            for when, _ in trial_drops:
                i = int(round((when - PRE_S) * fs))
                d["ice"]["psd"].append(_psd(x[i : i + m], fs))
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
            i0, i1 = int((a + POUR_TRIM_S) * fs), int((b - POUR_TRIM_S) * fs)
            d["stream"]["psd"] += [_psd(x[i : i + m], fs) for i in range(i0, i1 - m + 1, m)]
            if n == EXAMPLE:
                d["stream"]["trace"] = (tr, (a, b))
        d["freqs"] = np.fft.rfftfreq(m, d=1 / fs)
        d["quiet_db"] = quiet
        d["own_db"] = float(np.median([q for q in quiet if q is not None]))
    m = int(round(SEGMENT_S * fs))
    noise = nx.astype(np.float64)[int(impacts.SKIP_S * fs) :]
    out["noise_psd"] = np.mean([_psd(noise[i : i + m], fs)
                                for i in range(0, noise.size - m + 1, m)], axis=0)
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
    ax.text(x + width + 0.04, y, f"{value:.0f} dB", va=va, ha="left", fontsize=FONT_PT - 2,
            color=TEXT)


def _caption(fig, text: str) -> None:
    """The figure's caption, wrapped to its width, under a layout that leaves room for it."""
    width, height = fig.get_size_inches()
    lines = textwrap.wrap(text, int(width * CAPTION_CHARS_PER_IN))
    fig.text(0.0, 0.0, "\n".join(lines), fontsize=SMALL_PT, color=MUTED, ha="left",
             va="bottom")
    bottom = (len(lines) * SMALL_PT * 1.25 / 72 + 0.12) / height
    fig.get_layout_engine().set(rect=(0, bottom, 1, 1 - bottom))


def plot_levels(path: Path, data: dict, kinds: tuple[str, ...]) -> None:
    both = len(kinds) == 2
    fig = Figure(figsize=(12.4, 5.6) if both else (7.6, 5.6), layout="constrained")
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
        ax.set_title({"ice": "Ice drops: each drop's peak",
                      "stream": "Stream trials: median over each pour"}[kind], loc="left")
        _style_axes(ax)
    axes[0].set_ylabel("dB above the position's background")
    axes[0].set_ylim(0, 85 if "ice" in kinds else 32)
    if "ice" in kinds:
        axes[0].text(len(POSITIONS) - 0.22, threshold + 1.0, "detection threshold",
                     ha="right", va="bottom", color=TEXT_2, fontsize=FONT_PT - 2)
    else:
        axes[0].text(-0.5, threshold - 0.6, "detection threshold", ha="left", va="top",
                     color=TEXT_2, fontsize=FONT_PT - 2)
    own = ", ".join(f"{label.lower()} {data[pos]['own_db']:+.1f} dB"
                    for pos, _, label in POSITIONS)
    setup = ". ".join(SETUP[k][0].upper() + SETUP[k][1:] for k in kinds)
    _caption(fig, f"2026-11-04, sensor taped to a toilet. {setup}. Bar: median. Level: the "
             "detector's 5 ms RMS above 100 Hz, against each position's own background, the "
             "median level between events in its trials (against the noise recorded at the "
             f"base: {own}).")
    fig.savefig(path, dpi=300, metadata={"Software": None})


def plot_traces(path: Path, data: dict, kinds: tuple[str, ...]) -> None:
    both = len(kinds) == 2
    fig = Figure(figsize=(12.4, 6.6) if both else (12.4, 6.0), layout="constrained")
    axes = fig.subplots(len(POSITIONS), len(kinds), sharey=True, sharex="col", squeeze=False)
    threshold = impacts.DETECTION.threshold_db
    top = 85 if "ice" in kinds else 50
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
            ax.set_yticks([0, 40, 80] if top > 60 else [0, 20, 40])
            _style_axes(ax)
        row[0].set_ylabel(label, rotation=0, ha="right", va="center", labelpad=12)
    titles = {"ice": f"Ice trial {EXAMPLE}: 5 cubes (dots)",
              "stream": f"Stream trial {EXAMPLE}: one cup (shaded: steady pour)"}
    for ax, kind in zip(axes[0], kinds, strict=True):
        ax.set_title(titles[kind], loc="left")
    for ax in axes[-1]:
        ax.set_xlabel("time from start of recording (s)")
    _caption(fig, "dB above the noise baseline (recorded at the base), the detector's 5 ms RMS "
             "above 100 Hz; dashed: detection threshold (14 dB). The second trial "
             + ("of each kind " if both else "") + "at each position.")
    fig.savefig(path, dpi=300, metadata={"Software": None})


def plot_spectra(path: Path, data: dict, kinds: tuple[str, ...]) -> None:
    both = len(kinds) == 2
    grid = np.geomspace(F_MIN_HZ, F_MAX_HZ, 400)
    freqs = data[POSITIONS[0][0]]["freqs"]
    noise_db = 10 * np.log10(smooth(freqs, data["noise_psd"], grid))
    fig = Figure(figsize=(12.4, 5.0) if both else (9.6, 5.4), layout="constrained")
    axes = fig.subplots(1, len(kinds), sharey=True, squeeze=False)[0]
    for ax, kind in zip(axes, kinds, strict=True):
        ax.axvspan(*RESONANCE_BAND_HZ, color=BAND, alpha=0.12, linewidth=0)
        ax.plot(grid, noise_db, color=NOISE, linestyle=(0, (4, 3)), linewidth=2.0)
        for pos, color, _ in POSITIONS:
            psd = np.mean(data[pos][kind]["psd"], axis=0)
            ax.plot(grid, 10 * np.log10(smooth(freqs, psd, grid)), color=color, linewidth=2.5)
        ax.set_xscale("log")
        ax.set_xlim(F_MIN_HZ, F_MAX_HZ)
        ax.set_xticks([100, 1000, 10000], ["100 Hz", "1 kHz", "10 kHz"])
        ax.set_title({"ice": "Ice drops", "stream": "Stream trials, during the pour"}[kind],
                     loc="left")
        _style_axes(ax)
    axes[0].set_ylabel("power (dB re 1 count²/Hz)")
    axes[0].set_ylim(-52, 33 if "ice" in kinds else 20)  # room for the band label
    axes[0].text(sum(RESONANCE_BAND_HZ) / 2, -50, "sensor\nresonance", ha="center",
                 va="bottom", color=TEXT_2, fontsize=SMALL_PT + 2, linespacing=1.0)
    handles = [Line2D([], [], color=color, linewidth=2.5, label=label)
               for _, color, label in POSITIONS]
    handles.append(Line2D([], [], color=NOISE, linestyle=(0, (4, 3)), linewidth=2.0,
                          label="noise"))
    fig.legend(handles=handles, loc="outside upper right", ncol=4, frameon=False,
               fontsize=FONT_PT if both else FONT_PT - 2)
    what = {"ice": f"a {SEGMENT_S * 1000:.0f} ms window on each of the 15 drops per position "
                   f"(from {PRE_S * 1000:.0f} ms before it)",
            "stream": f"consecutive {SEGMENT_S * 1000:.0f} ms windows over the steady pour of "
                      "the 3 stream trials per position"}
    fig.get_layout_engine().set(wspace=0.06)
    _caption(fig, "Average spectrum of " + ", and of ".join(what[k] for k in kinds) + "; "
             "1/6-octave smoothed. Shaded: the sensor's 4 to 5 kHz resonance (datasheet). "
             f"Noise: {NOISE_ID}, recorded at the base.")
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
              f"dB, range {flat.min():5.1f} to {flat.max():5.1f}")
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
        for name, plot in (("levels", plot_levels), ("traces", plot_traces),
                           ("spectra", plot_spectra)):
            path = OUT_DIR / f"{prefix}_{name}.png"
            plot(path, data, kinds)
            print(path)


if __name__ == "__main__":
    main()
