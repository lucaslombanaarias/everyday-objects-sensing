"""Compare the bare sensor with the sensor in its enclosure, impact by impact.

    python scripts/compare_conditions.py data/controlled/raw/<date> [--out <folder>]

Trials must be named <floor>_<bare|enclosed>_<excitation>_<radius>m_<nn> (noise baselines
<floor>_<bare|enclosed>_noise_<nn>); others are skipped. For each floor, excitation and
radius it compares the two conditions on:

- Levels: each impact's det-v1 levels (the 5 ms RMS above 100 Hz, and the 50 ms RMS in
  100-500 Hz, 500-2000 Hz and 2-8 kHz), in dB re 1 count, i.e. 20*log10(RMS in counts).
  These do not depend on the noise baseline, so conditions measured against different
  baselines compare directly. Median and interquartile range over the impacts, the median of
  each trial, and enclosed minus bare. "separated" means every enclosed trial's median is
  above (or below) every bare trial's: the difference is larger than the trial-to-trial
  spread. "near_noise" means one condition's median is less than 6 dB above its own noise
  baseline in that band, so the difference there is partly a difference in noise floors.
- Spectra: the power spectral density of each impact (a 100 ms Hann window from 10 ms before
  its detection peak), averaged per condition, against the PSD of the noise baseline the
  trials were measured against. The enclosed-minus-bare difference is drawn only where both
  conditions are at least 6 dB above their noise.

Which impacts: for drops (one made per trial) the strongest event of each trial, which is
the drop; the weaker events around it are bounces or the release. For other excitations
every event that is not at an edge or tagged walk_in_out, other_noise or unknown.

Writes comparison.csv to the folder's analysis outputs and comparison_levels.png,
comparison_spectra.png and comparison_difference.png to figures/<date>/.
"""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
import numpy as np
from scipy import signal

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from everyday_sensing import impacts  # noqa: E402

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
MUTED = "#8a8985"
GRID = "#e6e5e1"
CONDITIONS = ("bare", "enclosed")
COLORS = {"bare": "#2a78d6", "enclosed": "#eb6834"}
"""Categorical slots 1 and 2, in fixed order: bare is always blue."""

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": MUTED, "axes.labelcolor": TEXT_2, "xtick.color": TEXT_2,
    "ytick.color": TEXT_2, "text.color": TEXT, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
    "font.size": 9,
})

ID = re.compile(r"^(?P<floor>[a-z]+)_(?P<cond>bare|enclosed)_(?P<kind>[a-z0-9]+?)"
                r"(?:_(?P<radius>[0-9p]+)m)?_(?P<n>\d+)$")
SPECTRUM_WINDOW_S = 0.1
SPECTRUM_LEAD_S = 0.01
SNR_FLOOR_DB = 6.0
F_MIN, F_MAX = 100.0, 20000.0
"""det-v1 and the bands start at 100 Hz; below it the 100 ms window's 10 Hz bins are too
coarse to smooth, and the 41 Hz hum dominates."""
EXCLUDED_TAGS = ("walk_in_out", "other_noise", "unknown")


def parse_id(trial_id: str) -> dict | None:
    m = ID.match(trial_id)
    if not m or (m["kind"] != "noise" and m["radius"] is None):
        return None
    radius = None if m["radius"] is None else float(m["radius"].replace("p", "."))
    return {"floor": m["floor"], "cond": m["cond"], "kind": m["kind"], "radius": radius}


def chosen_impacts(t: dict, events: list[dict]) -> list[dict]:
    """The impacts of one trial that stand for the excitation (see the module docstring)."""
    found = t["result"]["impacts"]
    if not found:
        return []
    if t["meta"].get("impacts_made") == 1:
        return [max(found, key=lambda i: i["levels_db"][impacts.DETECT])]
    by_time = {round(float(e["t_s"]), 3): e for e in events}
    keep = []
    for imp in found:
        e = by_time.get(round(imp["t_s"], 3))
        if e and (e["flag"] == "edge" or e["annotation"] in EXCLUDED_TAGS):
            continue
        keep.append(imp)
    return keep


def above_noise_db(imp: dict) -> dict:
    return {f"{name} above noise": imp["levels_db"][name] for name in impacts.REPORTED}


def absolute_db(imp: dict, baseline: dict) -> dict:
    """Each level of an impact in dB re 1 count, from its dB above the baseline."""
    return {name: imp["levels_db"][name] + 20 * np.log10(baseline[name])
            for name in impacts.REPORTED}


def impact_psd(x, fs, t_s) -> np.ndarray | None:
    """One-sided PSD (counts^2/Hz) of the window around one impact."""
    n = int(round(SPECTRUM_WINDOW_S * fs))
    start = int(round((t_s - SPECTRUM_LEAD_S) * fs))
    if start < 0 or start + n > x.size:
        return None
    _, p = signal.periodogram(x[start:start + n], fs, window="hann", detrend="constant")
    return p


def noise_psd(x, fs) -> np.ndarray:
    n = int(round(SPECTRUM_WINDOW_S * fs))
    _, p = signal.welch(x[int(impacts.SKIP_S * fs):], fs, window="hann", nperseg=n,
                        detrend="constant")
    return p


def smooth(freqs, psd, fraction=6, points=240):
    """Fractional-octave average of a PSD on a log frequency grid."""
    grid = np.geomspace(F_MIN, F_MAX, points)
    half = 2 ** (1 / (2 * fraction))
    out = np.array([psd[(freqs >= f / half) & (freqs < f * half)].mean()
                    if np.any((freqs >= f / half) & (freqs < f * half)) else np.nan
                    for f in grid])
    return grid, out


def collect(folder: Path) -> tuple[dict, dict, float]:
    """Per (floor, kind, radius, cond): impacts with levels and PSDs; noise PSD per trial."""
    trials = impacts.analyze_folder(folder)
    analysis = impacts.analysis_folder(folder)
    groups = defaultdict(lambda: {"levels": [], "trial_medians": {}, "psds": [],
                                  "baselines": set()})
    noises, skipped, fs = {}, [], None
    for t in trials:
        meta = t["meta"]
        key = parse_id(meta["trial_id"])
        if key is None:
            skipped.append(meta["trial_id"])
            continue
        _, x, fs = impacts.load_trial(folder / f"{meta['trial_id']}.json")
        if t["result"] is None:
            noises[meta["trial_id"]] = {"psd": noise_psd(x, fs), "baseline": t["noise_baseline"],
                                        "floor": key["floor"], "cond": key["cond"]}
            continue
        events_csv = analysis / f"events_{meta['trial_id']}.csv"
        events = impacts.read_events(events_csv) if events_csv.exists() else []
        g = groups[(key["floor"], key["kind"], key["radius"], key["cond"])]
        g["baselines"].add(t["result"]["baseline_source"])
        levels = [absolute_db(i, t["result"]["baseline"]) | above_noise_db(i)
                  for i in chosen_impacts(t, events)]
        g["levels"] += levels
        if levels:
            g["trial_medians"][meta["trial_id"]] = {
                name: float(np.median([lv[name] for lv in levels])) for name in impacts.REPORTED}
        for imp in chosen_impacts(t, events):
            p = impact_psd(x, fs, imp["t_s"])
            if p is not None:
                g["psds"].append(p)
    if skipped:
        print(f"skipped {len(skipped)} trials not named <floor>_<bare|enclosed>_...: "
              + ", ".join(skipped))
    return groups, noises, fs


def comparison_rows(groups: dict) -> list[dict]:
    rows = []
    for floor, kind, radius in sorted({k[:3] for k in groups}):
        pair = {c: groups.get((floor, kind, radius, c)) for c in CONDITIONS}
        if not all(pair.values()):
            continue
        for name in impacts.REPORTED:
            row = {"floor": floor, "excitation": kind, "radius_m": radius, "level": name}
            stats = {}
            for c in CONDITIONS:
                v = np.array([lv[name] for lv in pair[c]["levels"]])
                tm = sorted(m[name] for m in pair[c]["trial_medians"].values())
                q1, med, q3 = np.percentile(v, [25, 50, 75]) if v.size else [np.nan] * 3
                above = np.median([lv[f"{name} above noise"] for lv in pair[c]["levels"]])
                stats[c] = (med, tm, above)
                row.update({
                    f"{c}_impacts": v.size,
                    f"{c}_trials": len(tm),
                    f"{c}_median_db": round(med, 1),
                    f"{c}_iqr_db": round(q3 - q1, 1),
                    f"{c}_median_above_noise_db": round(float(above), 1),
                    f"{c}_trial_medians_db": " ".join(f"{m:.1f}" for m in tm),
                    f"{c}_baseline": " ".join(sorted(pair[c]["baselines"])),
                })
            diff = stats["enclosed"][0] - stats["bare"][0]
            b, e = stats["bare"][1], stats["enclosed"][1]
            row["enclosed_minus_bare_db"] = round(diff, 1)
            row["separated"] = "yes" if b and e and (min(e) > max(b) or max(e) < min(b)) else "no"
            near = min(stats[c][2] for c in CONDITIONS) < SNR_FLOOR_DB
            row["near_noise"] = "yes" if near else "no"
            rows.append(row)
    return rows


def panels(groups):
    """(floor, kind) rows and radius columns that have both conditions."""
    keys = sorted({k[:3] for k in groups
                   if all((k[0], k[1], k[2], c) in groups for c in CONDITIONS)})
    rows = list(dict.fromkeys((f, k) for f, k, _ in keys))
    radii = sorted({r for _, _, r in keys})
    return rows, radii


def plot_levels(groups: dict, rows: list, out: Path) -> Path:
    names = impacts.REPORTED
    fig, axes = plt.subplots(len(rows), len(names), figsize=(3.1 * len(names), 2.4 * len(rows)),
                             dpi=150, squeeze=False, sharey="row")
    radii = sorted({k[2] for k in groups})
    for (floor, kind), line in zip(rows, axes, strict=True):
        for j, (name, ax) in enumerate(zip(names, line, strict=True)):
            for c, dx in zip(CONDITIONS, (-0.12, 0.12), strict=True):
                for i, radius in enumerate(radii):
                    g = groups.get((floor, kind, radius, c))
                    if not g or not g["levels"]:
                        continue
                    v = np.array([lv[name] for lv in g["levels"]])
                    jitter = np.linspace(-0.05, 0.05, v.size) if v.size > 1 else np.zeros(1)
                    ax.scatter(i + dx + jitter, v, s=9, color=COLORS[c], alpha=0.55,
                               linewidths=0, label=c if (i == 0 and j == 0) else None)
                    ax.plot([i + dx - 0.09, i + dx + 0.09], [np.median(v)] * 2,
                            color=COLORS[c], linewidth=2)
            ax.set_xticks(range(len(radii)), [f"{r:g} m" for r in radii])
            ax.grid(axis="x", visible=False)
            if floor == rows[0][0] and kind == rows[0][1]:
                ax.set_title(name, loc="left", fontsize=9)
            if j == 0:
                ax.set_ylabel(f"{floor} {kind}\ndB re 1 count", fontsize=8)
    handles = [plt.Line2D([], [], color=COLORS[c], marker="o", linestyle="-", label=c)
               for c in CONDITIONS]
    fig.legend(handles=handles, loc="upper right", frameon=False, ncols=2)
    fig.suptitle("Impact levels, bare against enclosed (dots: impacts; bars: medians)",
                 x=0.01, ha="left", fontsize=12)
    fig.tight_layout()
    path = out / "comparison_levels.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def mean_psd(g) -> np.ndarray | None:
    return np.mean(g["psds"], axis=0) if g and g["psds"] else None


def noise_for(g, noises):
    """Mean PSD of the noise baselines the group's trials were measured against."""
    psds = [noises[b]["psd"] for b in g["baselines"] if b in noises]
    return np.mean(psds, axis=0) if psds else None


def plot_spectra(groups, noises, rows, radii, fs, out: Path) -> tuple[Path, Path]:
    n = int(round(SPECTRUM_WINDOW_S * fs))
    freqs = np.fft.rfftfreq(n, 1 / fs)
    figs = {k: plt.subplots(len(rows), len(radii), figsize=(3.6 * len(radii), 2.5 * len(rows)),
                            dpi=150, squeeze=False, sharex=True)
            for k in ("spectra", "difference")}
    for r, (floor, kind) in enumerate(rows):
        for c_i, radius in enumerate(radii):
            ax_s = figs["spectra"][1][r][c_i]
            ax_d = figs["difference"][1][r][c_i]
            smoothed = {}
            for c in CONDITIONS:
                g = groups.get((floor, kind, radius, c))
                p, q = mean_psd(g), noise_for(g, noises) if g else None
                if p is None:
                    continue
                f, ps = smooth(freqs, p)
                ax_s.plot(f, 10 * np.log10(ps), color=COLORS[c], linewidth=1.6,
                          label=f"{c} ({len(g['psds'])} impacts)")
                if q is not None:
                    _, qs = smooth(freqs, q)
                    ax_s.plot(f, 10 * np.log10(qs), color=COLORS[c], linewidth=0.9,
                              linestyle=(0, (3, 3)))
                    smoothed[c] = (ps, qs)
            if len(smoothed) == 2:
                (pb, qb), (pe, qe) = smoothed["bare"], smoothed["enclosed"]
                ok = (10 * np.log10(pb / qb) >= SNR_FLOOR_DB) & (
                    10 * np.log10(pe / qe) >= SNR_FLOOR_DB)
                d = np.where(ok, 10 * np.log10(pe / pb), np.nan)
                ax_d.plot(f, d, color=TEXT, linewidth=1.6)
                ax_d.axhline(0, color=MUTED, linewidth=1)
            for ax in (ax_s, ax_d):
                ax.set_xscale("log")
                ax.set_xlim(F_MIN, F_MAX)
                for lo, hi in impacts.BANDS.values():
                    ax.axvline(lo, color=GRID, linewidth=0.8, zorder=0)
                    ax.axvline(hi, color=GRID, linewidth=0.8, zorder=0)
                if r == 0:
                    ax.set_title(f"{radius:g} m", loc="left", fontsize=9)
                if r == len(rows) - 1:
                    ax.set_xlabel("frequency (Hz)")
            if c_i == 0:
                ax_s.set_ylabel(f"{floor} {kind}\nPSD dB re 1 count²/Hz", fontsize=8)
                ax_d.set_ylabel(f"{floor} {kind}\nenclosed − bare (dB)", fontsize=8)
            ax_s.legend(fontsize=7, frameon=False, loc="lower left")
    titles = {
        "spectra": "Averaged impact spectra, bare (blue) and enclosed (orange); "
                   "dashed: their noise baselines",
        "difference": "Enclosed minus bare spectrum, where both are at least "
                      f"{SNR_FLOOR_DB:g} dB above their noise",
    }
    paths = []
    for k, (fig, _) in figs.items():
        fig.suptitle(titles[k], x=0.01, ha="left", fontsize=12)
        fig.tight_layout()
        path = out / f"comparison_{k}.png"
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)
        paths.append(path)
    return tuple(paths)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="a raw/<date> folder of trials")
    parser.add_argument("--out", type=Path, help="default: figures/<date> next to raw/")
    args = parser.parse_args(argv)
    groups, noises, fs = collect(args.folder)
    rows = comparison_rows(groups)
    if not rows:
        raise SystemExit("no floor, excitation and radius has both bare and enclosed trials")
    csv_path = impacts.analysis_folder(args.folder) / "comparison.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(csv_path)

    out = args.out or (args.folder.parent.parent / "figures" / args.folder.name
                       if args.folder.parent.name == "raw" else args.folder / "figures")
    out.mkdir(parents=True, exist_ok=True)
    grid_rows, radii = panels(groups)
    print(plot_levels(groups, grid_rows, out))
    for p in plot_spectra(groups, noises, grid_rows, radii, fs, out):
        print(p)

    print("\nnoise floors (median 5 ms RMS above 100 Hz, dB re 1 count):")
    for name, n in sorted(noises.items()):
        print(f"  {name:28s} {20 * np.log10(n['baseline'][impacts.DETECT]):5.1f}")
    print("\nenclosed minus bare, dB (* = separated: every trial median of one condition "
          f"beyond every one of the other; ~ = near noise: a condition under {SNR_FLOOR_DB:g} dB "
          "above its noise)")
    print(f"  {'floor':7s}{'excitation':11s}{'radius':>7s}  "
          + "  ".join(f"{n:>20s}" for n in impacts.REPORTED))
    by_key = defaultdict(dict)
    for r in rows:
        by_key[(r["floor"], r["excitation"], r["radius_m"])][r["level"]] = r
    for (floor, kind, radius), levels in by_key.items():
        cells = [f"{levels[n]['enclosed_minus_bare_db']:+.1f}"
                 + ("*" if levels[n]["separated"] == "yes" else " ")
                 + ("~" if levels[n]["near_noise"] == "yes" else " ") for n in impacts.REPORTED]
        print(f"  {floor:7s}{kind:11s}{radius:>6g}m  " + "  ".join(f"{c:>20s}" for c in cells))


if __name__ == "__main__":
    main()
