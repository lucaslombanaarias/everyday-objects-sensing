"""Slide figures for the floor tests, bare sensor against enclosed.

Run as ``python scripts/plot_floor_slides.py``. Writes 300 dpi PNGs to docs/figures/:

- floor_enclosure_effect.png: enclosed minus bare level, per floor, excitation, radius and
  band (the median impact level of each condition, in dB re 1 count).
- floor_spectra_wood.png: averaged impact spectra on wood, bare against enclosed, with
  the sensor's datasheet resonance band.
- floor_level_vs_distance.png: the 5 ms detection level against radius, per condition.
- floor_detection.png: share of steps or stomps det-v1 finds, per condition and radius.

dashboard_window.png and dashboard_results_tab.png, next to them, are screenshots of the PCM
dashboard on wood_enclosed_walk_1m_03 (the live view on the left is the synthetic SIM stream).

The numbers come from scripts/compare_conditions.py (levels and spectra) and the det-v1
outputs in data/controlled/analysis/ (counts), for the sessions in SESSIONS.
"""

import csv
import textwrap
from pathlib import Path

import compare_conditions as cc
import matplotlib as mpl
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure

from everyday_sensing import impacts
from everyday_sensing.filter_report import (
    GRID_COLOR,
    RESONANCE_BAND_HZ,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

REPO = Path(__file__).resolve().parents[1]
SESSIONS = ("session_2026-10-18_evening", "2026-10-21")
OUT_DIR = REPO / "docs" / "figures"
FONT_PT = 16
COLORS = cc.COLORS
MUTED = "#8a8985"
NEUTRAL = "#f1f0ec"
LABELS = {"walk": "walking", "drop4in": "4 in drop", "stomp": "stomps"}
LEVEL_LABELS = {impacts.DETECT: "overall\n(5 ms, >100 Hz)", "100-500 Hz": "100–500 Hz",
                "500-2000 Hz": "500–2000 Hz", "2-8 kHz": "2–8 kHz"}

mpl.rcParams.update({
    "font.size": FONT_PT, "axes.labelsize": FONT_PT, "xtick.labelsize": FONT_PT - 2,
    "ytick.labelsize": FONT_PT - 2, "legend.fontsize": FONT_PT - 2,
    "axes.titlesize": FONT_PT, "axes.labelcolor": TEXT_PRIMARY,
    "xtick.color": TEXT_SECONDARY, "ytick.color": TEXT_SECONDARY,
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "axes.spines.top": False, "axes.spines.right": False,
})


def _style(ax) -> None:
    ax.grid(color=GRID_COLOR, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color(GRID_COLOR)


def _title(fig, text, sub=None):
    fig.suptitle(text, x=0.01, ha="left", fontsize=FONT_PT + 4, color=TEXT_PRIMARY,
                 fontweight="bold")
    if sub:
        # Below the axes; savefig's tight bounding box makes room for it.
        width = int(fig.get_figwidth() * 72 / ((FONT_PT - 4) * 0.55))
        fig.text(0.01, -0.01, textwrap.fill(sub, width), ha="left", va="top",
                 fontsize=FONT_PT - 4, color=MUTED)


def load():
    groups, rows, counts = {}, [], []
    for session in SESSIONS:
        folder = REPO / "data" / "controlled" / "raw" / session
        groups[session] = cc.collect(folder)
        g = groups[session][0]
        rows += cc.comparison_rows(g)
        with (impacts.analysis_folder(folder) / "impacts_counts.csv").open(encoding="utf-8") as f:
            counts += list(csv.DictReader(f))
    return groups, rows, counts


def plot_effect(rows, path):
    """Heatmap of enclosed minus bare per (floor, excitation, radius) and level."""
    keys = []
    order = {"tile": 0, "wood": 1, "carpet": 2}
    for r in rows:
        k = (r["floor"], r["excitation"], r["radius_m"])
        if k not in keys:
            keys.append(k)
    keys.sort(key=lambda k: (order.get(k[0], 9), k[1] != "walk" and k[1] != "stomp", k[2]))
    levels = list(impacts.REPORTED)
    by = {(r["floor"], r["excitation"], r["radius_m"], r["level"]): r for r in rows}
    data = np.array([[by[(*k, lv)]["enclosed_minus_bare_db"] for lv in levels] for k in keys])
    cmap = LinearSegmentedColormap.from_list("div", [COLORS["bare"], NEUTRAL, COLORS["enclosed"]])
    fig = Figure(figsize=(12, 1.5 + 0.48 * len(keys)), layout="constrained")
    ax = fig.subplots()
    im = ax.imshow(data, cmap=cmap, vmin=-15, vmax=15, aspect="auto")
    ax.grid(False)
    for i, k in enumerate(keys):
        for j, lv in enumerate(levels):
            r = by[(*k, lv)]
            near = r["near_noise"] == "yes"
            strong = r["separated"] == "yes"
            ax.text(j, i, f"{r['enclosed_minus_bare_db']:+.1f}" + ("~" if near else ""),
                    ha="center", va="center", fontsize=FONT_PT - 2,
                    color=MUTED if near else TEXT_PRIMARY,
                    fontweight="bold" if strong and not near else "normal",
                    fontstyle="italic" if near else "normal")
    ax.set_xticks(range(len(levels)), [LEVEL_LABELS[lv] for lv in levels])
    ax.xaxis.tick_top()
    ax.set_yticks(range(len(keys)),
                  [f"{f} · {LABELS.get(e, e)} · {r:g} m" for f, e, r in keys])
    for i in range(1, len(keys)):
        if keys[i][:2] != keys[i - 1][:2]:
            ax.axhline(i - 0.5, color="white", linewidth=4)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(length=0)
    cb = fig.colorbar(im, ax=ax, shrink=0.6, pad=0.02)
    cb.set_label("enclosed − bare (dB)")
    cb.outline.set_visible(False)
    _title(fig, "The enclosure reshapes the signal: more 2–8 kHz, less 0.5–2 kHz on wood",
           "Median impact level, enclosed minus bare, in dB. Bold: every enclosed trial differs "
           "from every bare one in the same direction. Grey ~: a condition is under 6 dB above "
           "its noise.")
    fig.savefig(path, dpi=300, bbox_inches="tight", pad_inches=0.15)


def plot_spectra(groups, path):
    g, noises, fs = groups["2026-10-21"]
    n = int(round(cc.SPECTRUM_WINDOW_S * fs))
    freqs = np.fft.rfftfreq(n, 1 / fs)
    cases = [(("wood", "walk", 1.0), "Walking, 1 m"), (("wood", "drop4in", 2.0), "4 in drop, 2 m")]
    fig = Figure(figsize=(13, 5.6), layout="constrained")
    axes = fig.subplots(1, len(cases), sharey=False)
    for ax, ((floor, kind, radius), label) in zip(axes, cases, strict=True):
        ax.axvspan(*RESONANCE_BAND_HZ, color=GRID_COLOR, alpha=0.8, zorder=0)
        ax.text(np.sqrt(np.prod(RESONANCE_BAND_HZ)), 0.02, "sensor\nresonance",
                transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                fontsize=FONT_PT - 5, color=TEXT_SECONDARY)
        noise_drawn = False
        for c in cc.CONDITIONS:
            grp = g[(floor, kind, radius, c)]
            f, ps = cc.smooth(freqs, cc.mean_psd(grp))
            ax.plot(f, 10 * np.log10(ps), color=COLORS[c], linewidth=2.2)
            k = int(np.nanargmax(np.where(f > 2500, ps, np.nan)))
            q = cc.noise_for(grp, noises)
            if q is not None and not noise_drawn:
                _, qs = cc.smooth(freqs, q)
                ax.plot(f, 10 * np.log10(qs), color=MUTED, linewidth=1.4, linestyle=(0, (3, 3)))
                ax.text(f[-60], 10 * np.log10(qs[-60]) - 6, "noise", color=MUTED,
                        fontsize=FONT_PT - 4)
                noise_drawn = True
            ax.annotate(c, (f[k], 10 * np.log10(ps[k])), textcoords="offset points",
                        xytext=(8, 8 if c == "enclosed" else -22), color=COLORS[c],
                        fontsize=FONT_PT - 2, fontweight="bold")
        ax.set_xscale("log")
        ax.set_xlim(cc.F_MIN, cc.F_MAX)
        ax.set_xlabel("frequency (Hz)")
        ax.set_title(f"Wood, {label.lower()} ({len(grp['psds'])} impacts per condition)",
                     loc="left")
        _style(ax)
    axes[0].set_ylabel("power spectral density\n(dB re 1 count²/Hz)")
    _title(fig, "On wood, the enclosure boosts 3–6 kHz, around the sensor's resonance",
           "Averaged spectrum of each impact (100 ms Hann window), 2026-10-21. Dashed: the noise "
           "baseline. Shaded: 4–5 kHz resonance from the sensor datasheet.")
    fig.savefig(path, dpi=300, bbox_inches="tight", pad_inches=0.15)


def plot_level(groups, path):
    cases = [("tile", "stomp"), ("wood", "walk"), ("wood", "drop4in"), ("carpet", "walk"),
             ("carpet", "drop4in")]
    fig = Figure(figsize=(16, 5.2), layout="constrained")
    axes = fig.subplots(1, len(cases))
    for ax, (floor, kind) in zip(axes, cases, strict=True):
        for session in SESSIONS:
            g = groups[session][0]
            for c in cc.CONDITIONS:
                radii = sorted(k[2] for k in g if k[:2] == (floor, kind) and k[3] == c)
                if not radii:
                    continue
                med = [np.median([lv[impacts.DETECT] for lv in g[(floor, kind, r, c)]["levels"]])
                       for r in radii]
                for r in radii:
                    tm = [m[impacts.DETECT]
                          for m in g[(floor, kind, r, c)]["trial_medians"].values()]
                    ax.scatter([r] * len(tm), tm, s=22, color=COLORS[c], alpha=0.35, linewidths=0)
                ax.plot(radii, med, color=COLORS[c], linewidth=2.2, marker="o", markersize=8,
                        label=c)
        ax.set_title(f"{floor}, {LABELS[kind]}", loc="left")
        ax.set_xticks([1, 2, 3])
        ax.set_xlim(0.6, 3.4)
        ax.set_xlabel("radius (m)")
        _style(ax)
    axes[0].set_ylabel("impact level\n(dB re 1 count, 5 ms >100 Hz)")
    axes[0].legend(frameon=False, loc="lower left")
    _title(fig, "Overall level falls with distance; the enclosure moves it a few dB, more on tile",
           "Lines: median over impacts. Dots: each trial's median. Tile: hand stomps, 10/18; "
           "others: 2026-10-21.")
    fig.savefig(path, dpi=300, bbox_inches="tight", pad_inches=0.15)


def plot_detection(counts, path):
    cases = [("tile", "stomp"), ("wood", "walk"), ("carpet", "walk")]
    fig = Figure(figsize=(13, 5.2), layout="constrained")
    axes = fig.subplots(1, len(cases), sharey=True)
    for ax, (floor, kind) in zip(axes, cases, strict=True):
        radii = []
        for c_i, c in enumerate(cc.CONDITIONS):
            rows = [r for r in counts if r["trial_id"].startswith(f"{floor}_{c}_{kind}_")]
            by_r = {}
            for r in rows:
                rad = float(r["distance_m"])
                found = int(r["impacts_found"]) - int(r["walk_in_out"] or 0) - int(
                    r["other_noise"] or 0)
                by_r.setdefault(rad, [0, 0])
                by_r[rad][0] += found
                by_r[rad][1] += int(r["impacts_made"])
            radii = sorted(by_r)
            xs = np.arange(len(radii)) + (c_i - 0.5) * 0.38
            pct = [100 * by_r[r][0] / by_r[r][1] for r in radii]
            ax.bar(xs, pct, width=0.36, color=COLORS[c], label=c, edgecolor="white",
                   linewidth=2)
            for x, p in zip(xs, pct, strict=True):
                ax.text(x, p + 2, f"{p:.0f}%", ha="center", va="bottom", fontsize=FONT_PT - 5,
                        color=TEXT_SECONDARY)
        ax.axhline(100, color=TEXT_SECONDARY, linewidth=1.2, linestyle=(0, (5, 4)))
        ax.set_xticks(range(len(radii)), [f"{r:g} m" for r in radii])
        ax.set_ylim(0, 130)
        ax.set_title(f"{floor}, {LABELS[kind]}", loc="left")
        _style(ax)
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("events found / impacts made")
    axes[-1].legend(frameon=False, loc="upper right", ncols=2)
    _title(fig, "det-v1 finds nearly every step and stomp on wood and tile; "
                "carpet loses steps at 3 m",
           "Events found per impact made, all trials pooled (3 per bar). Events tagged as noise "
           "are left out. Above 100%: extra events (double counts, bounces).")
    fig.savefig(path, dpi=300, bbox_inches="tight", pad_inches=0.15)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    groups, rows, counts = load()
    for name, fn, arg in (("floor_enclosure_effect.png", plot_effect, rows),
                          ("floor_spectra_wood.png", plot_spectra, groups),
                          ("floor_level_vs_distance.png", plot_level, groups),
                          ("floor_detection.png", plot_detection, counts)):
        fn(arg, OUT_DIR / name)
        print(OUT_DIR / name)


if __name__ == "__main__":
    main()
