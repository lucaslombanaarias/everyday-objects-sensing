"""Save one PNG per trial and a found-against-made summary for a folder of trials.

    python scripts/plot_trials.py data/raw/<date> [--out <folder>]

Each trial figure is the dashboard's Results plot: the RMS over the 5 ms detection window
against the det-v1 threshold, the detected events (numbered; tagged ones labeled with their
tag), the peaks that stayed below the threshold (hollow circles, with their level), the
bursts merged into a stronger event within the minimum gap (hollow squares), and the
high-passed waveform drawn faint behind them. Tags come from the events_<trial>.csv files
of the folder's analysis outputs (everyday_sensing.impacts.analysis_folder). Figures go to
figures/<date>/ next to raw/ and analysis/ by default.
"""

import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

from everyday_sensing import impacts  # noqa: E402

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
MUTED = "#8a8985"
GRID = "#e6e5e1"
WAVE = "#2a78d6"
RMS = "#eb6834"
EVENT = "#1baf7a"
EDGE = "#eda100"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": MUTED, "axes.labelcolor": TEXT_2, "xtick.color": TEXT_2,
    "ytick.color": TEXT_2, "text.color": TEXT, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
    "font.size": 10,
})


def figures_folder(folder: Path) -> Path:
    folder = Path(folder)
    if folder.parent.name == "raw":
        return folder.parent.parent / "figures" / folder.name
    return folder / "figures"


def describe(meta: dict) -> str:
    if meta.get("noise_only"):
        return "noise only"
    radius = meta.get("distance_m")
    return f"{meta['excitation']['type']} at {radius:g} m" if radius is not None else (
        meta["excitation"]["type"])


def plot_trial(t: dict, events: list[dict], folder: Path, out: Path) -> Path:
    meta = t["meta"]
    _, x, fs = impacts.load_trial(folder / f"{meta['trial_id']}.json")
    baseline = t["noise_baseline"] if t["result"] is None else t["result"]["baseline"]
    tr = impacts.detection_traces(x, fs, baseline)
    skipped = impacts.skipped_peaks(tr)
    row = impacts.count_row(t, events)

    fig, ax = plt.subplots(figsize=(11, 4.2), dpi=150)
    peak_rms = max(float(tr["rms"].max()), tr["threshold_counts"])
    top = 1.3 * peak_rms
    ax.plot(tr["t_s"], tr["highpassed"], color=WAVE, alpha=0.22, linewidth=0.6,
            label="high-passed waveform (clipped to the plot)", rasterized=True)
    ax.plot(tr["rms_t_s"], tr["rms"], color=RMS, linewidth=1.8, label="5 ms RMS")
    ax.axhline(tr["threshold_counts"], color=TEXT_2, linestyle=(0, (5, 4)), linewidth=1.2,
               label=f"threshold {impacts.THRESHOLD_DB:g} dB (5 ms RMS)")

    def at(db):
        return tr["baseline"] * 10 ** (db / 20)

    # Every event is labelled with its number only; the marker shows what it is, and the
    # legend names it, so close events do not collide.
    for kind, face, marker in KINDS:
        chosen = [e for e in events if _kind(e) == kind]
        if not chosen:
            continue
        ys = [at(float(e["peak_db"])) for e in chosen]
        ax.scatter([float(e["t_s"]) for e in chosen], ys, s=60, color=face, marker=marker,
                   zorder=4, edgecolors=SURFACE, linewidths=1.5, label=kind)
        for e, y in zip(chosen, ys, strict=True):
            ax.annotate(e["event"], (float(e["t_s"]), y), textcoords="offset points",
                        xytext=(0, 7), ha="center", fontsize=8, color=TEXT_2)
    merged = impacts.merged_peaks(tr, [float(e["t_s"]) for e in events])
    if merged:
        ys = [at(db) for _, db in merged]
        ax.scatter([m for m, _ in merged], ys, s=55, marker="s", facecolors="none",
                   edgecolors=TEXT_2, linewidths=1.3, zorder=4,
                   label=f"merged into a stronger event (< {impacts.MIN_GAP_S:g} s)")
    if skipped:
        ys = [at(db) for _, db in skipped]
        ax.scatter([s for s, _ in skipped], ys, s=60, facecolors="none", edgecolors=TEXT_2,
                   linewidths=1.3, zorder=4, label="peak below threshold")
        for (when, db), y in zip(skipped, ys, strict=True):
            ax.annotate(f"{db:.1f} dB", (when, y), textcoords="offset points", xytext=(0, 7),
                        ha="center", fontsize=8, color=TEXT_2)

    ax.set_ylim(-0.55 * top, top)
    ax.set_xlim(0, tr["t_s"][-1] if tr["t_s"].size else 1)
    ax.set_xlabel("time from start of recording (s)")
    ax.set_ylabel("counts")
    if t["result"] is None:
        counts = (f"{row['impacts_found']} false alarms in {row['duration_s']:.0f} s "
                  f"({row['false_alarms_per_min']:.1f} per minute)")
    else:
        counts = f"made {row['impacts_made']}, found {row['impacts_found']}"
        if row["difference"]:
            counts += f" ({row['difference']:+d})"
    ax.set_title(f"{meta['trial_id']}: {describe(meta)} — {counts}", loc="left",
                 fontsize=12, color=TEXT)
    fig.text(0.0, 0.0, f"det-v1, baseline {row['baseline_used']} "
             f"({baseline[impacts.DETECT]:.2f} counts), mount {row['mount'] or '-'}",
             fontsize=8, color=MUTED, ha="left", va="bottom", transform=fig.transFigure)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=False, fontsize=8)
    fig.tight_layout()
    path = out / f"{meta['trial_id']}.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


KINDS = (("event", EVENT, "o"), ("edge event", EDGE, "o"),
         ("tagged other_noise", MUTED, "o"), ("tagged walk_in_out", MUTED, "^"),
         ("tagged unknown", MUTED, "D"))
"""(legend label, colour, marker) for each kind of event."""


def _kind(event: dict) -> str:
    if event["annotation"] not in ("unreviewed", "true_impact"):
        return f"tagged {event['annotation']}"
    return "edge event" if event["flag"] == "edge" else "event"


def plot_summary(rows: list[dict], out: Path) -> Path:
    """Events found per trial against impacts made: one panel per condition (trial ID
    prefix), one row per excitation and impacts made, so conditions with the same
    excitation sit side by side on a shared scale, each trial a bar grouped by radius.
    Panels are in prefix order, so <floor>_bare_* sits next to <floor>_enclosed_*."""
    trials = [r for r in rows if r["impacts_made"]]
    conditions = sorted({r["trial_id"].rsplit("_", 2)[0] for r in trials})
    by_cond = {c: [r for r in trials if r["trial_id"].startswith(c + "_")] for c in conditions}
    row_keys = list(dict.fromkeys((g[0]["excitation"], g[0]["impacts_made"])
                                  for g in by_cond.values()))
    grid = [[c for c in conditions
             if (by_cond[c][0]["excitation"], by_cond[c][0]["impacts_made"]) == key]
            for key in row_keys]
    ncols = max(len(line) for line in grid)
    fig, axes = plt.subplots(len(grid), ncols, figsize=(2.7 * ncols + 0.8, 2.9 * len(grid)),
                             dpi=150, squeeze=False)
    for (excitation, made), line, row_axes in zip(row_keys, grid, axes, strict=True):
        group = [r for c in line for r in by_cond[c]]
        radii = sorted({r["distance_m"] for r in group})
        slots = {d: max(sum(r["distance_m"] == d for r in by_cond[c]) for c in line)
                 for d in radii}
        top = max(made, max(r["impacts_found"] for r in group)) * 1.3
        for i, ax in enumerate(row_axes):
            if i >= len(line):
                ax.set_visible(False)
                continue
            x0, ticks = 0.0, []
            for radius in radii:
                at_radius = [r for r in by_cond[line[i]] if r["distance_m"] == radius]
                for k, r in enumerate(at_radius):
                    found = r["impacts_found"]
                    explained = sum(r[tag] for tag in ("walk_in_out", "other_noise"))
                    ax.bar(x0 + k, found, width=0.8, color=WAVE, edgecolor=SURFACE, linewidth=2)
                    if explained:
                        ax.bar(x0 + k, explained, bottom=found - explained, width=0.8,
                               color="none", edgecolor=SURFACE, hatch="////", linewidth=0)
                    text = f"{found}" + (f"\n({explained} tagged)" if explained else "")
                    ax.annotate(text, (x0 + k, found), textcoords="offset points",
                                xytext=(0, 2), ha="center", va="bottom", fontsize=7,
                                color=TEXT_2)
                ticks.append((x0 + (slots[radius] - 1) / 2, f"{radius:g} m"))
                x0 += slots[radius] + 0.8
            ax.axhline(made, color=TEXT_2, linestyle=(0, (5, 4)), linewidth=1.2)
            ax.set_xticks([p for p, _ in ticks], [lbl for _, lbl in ticks])
            ax.set_xlim(-0.7, x0 - 1.1)
            ax.set_ylim(0, top)
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            ax.grid(axis="x", visible=False)
            ax.set_title(line[i].replace("_", " "), loc="left", fontsize=9, color=TEXT)
            if i == 0:
                ax.set_ylabel(f"{excitation}: found\n(dashed: {made} made)", fontsize=8)
            else:
                ax.tick_params(labelleft=False)
    fig.suptitle("Events found per trial against impacts made (det-v1), by radius",
                 x=0.01, ha="left", fontsize=12, color=TEXT)
    fig.tight_layout()
    path = out / "summary_found_vs_made.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="a raw/<date> folder of trials")
    parser.add_argument("--out", type=Path, help="default: figures/<date> next to raw/")
    args = parser.parse_args(argv)
    trials = impacts.analyze_folder(args.folder)
    analysis = impacts.write_outputs(trials, impacts.analysis_folder(args.folder))
    out = args.out or figures_folder(args.folder)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for t in trials:
        events = impacts.read_events(analysis / f"events_{t['meta']['trial_id']}.csv")
        rows.append(impacts.count_row(t, events))
        print(plot_trial(t, events, args.folder, out))
    if any(r["impacts_made"] for r in rows):  # a folder of noise trials has no summary
        print(plot_summary(rows, out))
    with (analysis / "impacts_counts.csv").open(encoding="utf-8") as f:
        print(f"{len(list(csv.DictReader(f)))} trials in {analysis / 'impacts_counts.csv'}")


if __name__ == "__main__":
    main()
