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
CONDITION_COLORS = ("#2a78d6", "#eb6834")
"""Categorical slots 1 and 2, in fixed order: the first condition is always blue."""

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

    for kind, face in (("event", EVENT), ("edge event", EDGE), ("tagged extra", MUTED)):
        chosen = [e for e in events if _kind(e) == kind]
        if not chosen:
            continue
        ys = [at(float(e["peak_db"])) for e in chosen]
        ax.scatter([float(e["t_s"]) for e in chosen], ys, s=60, color=face, zorder=4,
                   edgecolors=SURFACE, linewidths=1.5, label=kind)
        for e, y in zip(chosen, ys, strict=True):
            text = e["event"] if kind != "tagged extra" else f"{e['event']}: {e['annotation']}"
            ax.annotate(text, (float(e["t_s"]), y), textcoords="offset points",
                        xytext=(0, 7), ha="center", fontsize=8, color=TEXT_2)
    merged = impacts.merged_peaks(tr, [float(e["t_s"]) for e in events])
    if merged:
        ys = [at(db) for _, db in merged]
        ax.scatter([m for m, _ in merged], ys, s=55, marker="s", facecolors="none",
                   edgecolors=TEXT_2, linewidths=1.3, zorder=4,
                   label=f"merged into a stronger event (< {impacts.MIN_GAP_S:g} s)")
        for (when, _db), y in zip(merged, ys, strict=True):
            ax.annotate("merged", (when, y), textcoords="offset points", xytext=(0, 7),
                        ha="center", fontsize=8, color=TEXT_2)
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


def _kind(event: dict) -> str:
    if event["annotation"] not in ("unreviewed", "true_impact"):
        return "tagged extra"
    return "edge event" if event["flag"] == "edge" else "event"


def plot_summary(rows: list[dict], out: Path) -> Path:
    """Events found per trial against impacts made, grouped by condition and radius."""
    trials = [r for r in rows if r["impacts_made"]]
    conditions = list(dict.fromkeys(r["trial_id"].rsplit("_", 2)[0] for r in trials))
    radii = sorted({r["distance_m"] for r in trials})
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=150)
    width, x0, ticks = 0.8, 0.0, []
    for radius in radii:
        start = x0
        for c, cond in enumerate(conditions[:2]):
            group = [r for r in trials
                     if r["distance_m"] == radius and r["trial_id"].startswith(cond + "_")]
            for r in group:
                found = r["impacts_found"]
                explained = sum(r[tag] for tag in ("walk_in_out", "other_noise"))
                ax.bar(x0, found, width=width, color=CONDITION_COLORS[c],
                       edgecolor=SURFACE, linewidth=2,
                       label=cond.replace("_", " ") if (radius == radii[0] and r is group[0])
                       else None)
                if explained:
                    ax.bar(x0, explained, bottom=found - explained, width=width, color="none",
                           edgecolor=SURFACE, hatch="////", linewidth=0)
                text = f"{found}" + (f"\n({explained} tagged)" if explained else "")
                ax.annotate(text, (x0, found), textcoords="offset points", xytext=(0, 3),
                            ha="center", va="bottom", fontsize=8, color=TEXT_2)
                x0 += 1
            x0 += 0.6
        ticks.append(((start + x0 - 1.6) / 2, f"{radius:g} m"))
        x0 += 0.8
    made = trials[0]["impacts_made"]
    ax.axhline(made, color=TEXT_2, linestyle=(0, (5, 4)), linewidth=1.2,
               label=f"impacts made ({made})")
    ax.set_xticks([p for p, _ in ticks], [lbl for _, lbl in ticks])
    ax.set_xlabel("radius (impact point to sensor), 3 trials per condition")
    ax.set_ylabel("events found (det-v1)")
    ax.grid(axis="x", visible=False)
    ax.set_ylim(0, max(r["impacts_found"] for r in trials) * 1.25)
    ax.set_title("Events found per trial against impacts made", loc="left", fontsize=12)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=False, fontsize=9)
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
