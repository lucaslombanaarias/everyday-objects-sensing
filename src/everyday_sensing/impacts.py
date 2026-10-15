"""Find impacts (footsteps, stomps, drops) in recorded trials and summarize them by distance.

Run as ``python -m everyday_sensing.impacts data/raw/<date>`` to print one row per trial
and one per distance, and write impacts_trials.csv and impacts_summary.csv into that
folder. The PCM dashboard (tools/dashboard/vpu_pcm_dashboard.py) runs the same analysis
after every recording.

Detection: the 5 ms RMS of the signal above 100 Hz, in dB above a noise baseline. An
impact is a peak of it that reaches THRESHOLD_DB and is at least MIN_GAP_S after the
previous one. Footsteps on the tile floor arrive as clicks a few ms long; a 50 ms window
(used before 2026-10-14 23:10) averaged most of them away at 2 m.

Each impact is also reported in BANDS, from the 50 ms RMS (as in the stomp and first
footstep checks), so levels stay comparable with those numbers.

The baseline is the median envelope of the most recent noise-only trial recorded at or
before the trial in the same folder (any noise-only trial if none precedes it, else the
quietest 2 s of the trial itself); another noise-only trial can be chosen instead.
"""

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np
import scipy.io
from scipy import signal

from everyday_sensing.events import BandEnvelope

DETECT = "above 100 Hz (5 ms)"
DETECT_BAND = (100.0, None)
DETECT_WINDOW_S = 0.005
DETECT_RATE_HZ = 1000.0
BANDS = {"100-500 Hz": (100.0, 500.0), "500-2000 Hz": (500.0, 2000.0), "2-8 kHz": (2000.0, 8000.0)}
BAND_WINDOW_S = 0.05
BAND_RATE_HZ = 200.0
REPORTED = (DETECT, *BANDS)
"""Levels reported per impact: the detection level first, then the 50 ms band levels."""
THRESHOLD_DB = 14.0
"""Set from the trials of 2026-10-14: at 14 dB the detector fired 1.3 to 4.1 times per
minute on the three noise-only recordings, and found 7 of 9 firm steps in fs_2m_01 (16 dB:
3 of 9) and 9 of 9 stomping steps in fs_2m_03 and fs_2m_04."""
MIN_GAP_S = 0.25
"""Shortest time between two impacts; the 2 m steps on 2026-10-14 were 0.5 s apart."""
PEAK_WINDOW_S = 0.05
"""Half-width of the window in which each band's level is read around a detected peak."""
QUIET_WINDOW_S = 2.0
SKIP_S = 1.0
"""Start of a recording left out of its noise baseline (filter start-up)."""


def envelopes(x, fs) -> dict:
    """{name: (rate, RMS envelope in counts)} for the detection level and each band."""
    x = np.asarray(x, dtype=np.float64)
    specs = {DETECT: (DETECT_BAND, DETECT_WINDOW_S, DETECT_RATE_HZ)}
    specs.update({name: (band, BAND_WINDOW_S, BAND_RATE_HZ) for name, band in BANDS.items()})
    out = {}
    for name, (band, window, rate) in specs.items():
        env = BandEnvelope(fs, band, window_s=window, out_rate_hz=rate)
        out[name] = (env.rate, env.process(x))
    return out


def noise_baseline(x, fs) -> dict:
    """Median envelope per level of a noise-only recording, skipping its first SKIP_S."""
    out = {}
    for name, (rate, e) in envelopes(x, fs).items():
        skip = int(SKIP_S * rate)
        out[name] = float(np.median(e[skip:] if e.size > skip else e))
    return out


def quiet_baseline(envs: dict) -> dict:
    """Per level, the lowest median over QUIET_WINDOW_S windows (the quietest stretch)."""
    out = {}
    for name, (rate, e) in envs.items():
        w = max(1, int(QUIET_WINDOW_S * rate))
        hop = max(1, w // 8)
        starts = range(0, max(1, e.size - w + 1), hop)
        out[name] = float(min(np.median(e[i : i + w]) for i in starts))
    return out


def find_impacts(
    x,
    fs,
    baseline: dict | None = None,
    threshold_db: float = THRESHOLD_DB,
    min_gap_s: float = MIN_GAP_S,
) -> dict:
    """Impacts in one recording. Returns the baseline used and one dict per impact with
    its time and its level (dB above baseline) for each name in REPORTED."""
    envs = envelopes(x, fs)
    source = "given"
    if baseline is None:
        baseline, source = quiet_baseline(envs), "quietest 2 s of the trial"
    tiny = np.finfo(np.float64).tiny
    levels = {
        name: (rate, 20 * np.log10(np.maximum(e, tiny) / max(baseline[name], tiny)))
        for name, (rate, e) in envs.items()
    }
    rate, detect = levels[DETECT]
    peaks, _ = signal.find_peaks(
        detect, height=threshold_db, distance=max(1, int(min_gap_s * rate))
    )
    impacts = []
    for p in peaks:
        t = float((p + 1) / rate)
        row = {DETECT: float(detect[p])}
        for name in BANDS:
            band_rate, lv = levels[name]
            centre, half = int(round(t * band_rate)) - 1, max(1, int(PEAK_WINDOW_S * band_rate))
            row[name] = float(lv[max(0, centre - half) : centre + half + 1].max())
        impacts.append({"t_s": t, "levels_db": row})
    return {"baseline": baseline, "baseline_source": source, "impacts": impacts}


# --------------------------------------------------------------------------- folders


def load_trial(json_path: Path) -> tuple[dict, np.ndarray, float]:
    meta = json.loads(Path(json_path).read_text(encoding="utf-8"))
    mat = scipy.io.loadmat(Path(json_path).with_name(meta["files"]["mat"]))
    return meta, mat["counts"].ravel(), float(np.squeeze(mat["fs_hz"]))


def _try_load(json_path: Path):
    """load_trial, or None if the trial's files cannot be read yet: sidecars written by
    older versions of acquisition.record could appear before their .mat was complete."""
    try:
        return load_trial(json_path)
    except Exception:  # a partial .mat fails in several ways (KeyError, zlib, MatReadError)
        return None


def is_noise(meta: dict) -> bool:
    return bool(meta.get("noise_only")) or (meta.get("excitation") or {}).get("type") == "none"


def trial_jsons(folder: Path) -> list[Path]:
    """Sidecars written by acquisition.record in folder, oldest first."""
    found = []
    for path in Path(folder).glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict) and "checks" in meta and "files" in meta:
            found.append((meta.get("start_time", ""), path))
    return [p for _, p in sorted(found)]


def analyze_folder(
    folder,
    threshold_db: float = THRESHOLD_DB,
    cache: dict | None = None,
    baseline_trial: str | None = None,
):
    """Analyze every trial in folder. Returns one dict per trial, oldest first: "meta" (the
    sidecar) and either "result" (find_impacts output) or, for noise-only trials,
    "noise_baseline" (median envelope per band) with "result" None.

    baseline_trial names the noise-only trial to measure every trial against; None picks,
    per trial, the latest noise-only trial recorded before it. cache keeps results keyed
    by file, modification time, threshold and baseline trial, so repeated calls only
    analyze what changed."""
    cache = {} if cache is None else cache
    paths = trial_jsons(folder)
    noises = []
    trials = []
    for path in paths:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if is_noise(meta):
            key = ("noise", str(path), path.stat().st_mtime)
            if key not in cache:
                loaded = _try_load(path)
                if loaded is None:
                    continue
                cache[key] = noise_baseline(loaded[1], loaded[2])
            noises.append((meta.get("start_time", ""), meta["trial_id"], cache[key]))
            trials.append({"meta": meta, "noise_baseline": cache[key], "result": None})
    chosen = None
    if baseline_trial is not None:
        chosen = next((n for n in noises if n[1] == baseline_trial), None)
        if chosen is None:
            raise ValueError(f"no noise-only trial named {baseline_trial!r} in {folder}")
    for path in paths:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if is_noise(meta):
            continue
        if chosen is not None:
            ref = chosen
        else:
            before = [n for n in noises if n[0] <= meta.get("start_time", "")]
            ref = before[-1] if before else (noises[0] if noises else None)
        key = (str(path), path.stat().st_mtime, threshold_db, ref[1] if ref else None)
        if key not in cache:
            loaded = _try_load(path)
            if loaded is None:
                continue
            result = find_impacts(loaded[1], loaded[2], ref[2] if ref else None, threshold_db)
            if ref:
                result["baseline_source"] = ref[1]
            cache[key] = result
        trials.append({"meta": meta, "result": cache[key]})
    trials.sort(key=lambda t: t["meta"].get("start_time", ""))
    return trials


def _stats(values) -> dict:
    v = np.asarray(values, dtype=np.float64)
    if not v.size:
        return {"mean": math.nan, "std": math.nan, "min": math.nan, "max": math.nan}
    return {
        "mean": float(v.mean()),
        "std": float(v.std(ddof=1)) if v.size > 1 else math.nan,
        "min": float(v.min()),
        "max": float(v.max()),
    }


def summarize(trials: list[dict]) -> list[dict]:
    """One row per (excitation type, distance) over the non-noise trials."""
    groups: dict = {}
    for t in trials:
        if t["result"] is None:
            continue
        meta = t["meta"]
        key = ((meta.get("excitation") or {}).get("type") or "", meta.get("distance_m"))
        groups.setdefault(key, []).append(t)
    rows = []
    for (kind, distance), group in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        impacts = [i for t in group for i in t["result"]["impacts"]]
        made = [t["meta"].get("impacts_made") for t in group]
        row = {
            "excitation": kind,
            "distance_m": distance,
            "trials": len(group),
            "impacts_found": len(impacts),
            "impacts_made": sum(made) if all(m is not None for m in made) else None,
        }
        for band in REPORTED:
            s = _stats([i["levels_db"][band] for i in impacts])
            row.update({f"{band} {k} dB": v for k, v in s.items()})
        rows.append(row)
    return rows


def trial_rows(trials: list[dict]) -> list[dict]:
    rows = []
    for t in trials:
        meta = t["meta"]
        if t["result"] is None:
            continue
        for k, imp in enumerate(t["result"]["impacts"], 1):
            rows.append(
                {
                    "trial_id": meta["trial_id"],
                    "excitation": (meta.get("excitation") or {}).get("type"),
                    "distance_m": meta.get("distance_m"),
                    "baseline": t["result"]["baseline_source"],
                    "impact": k,
                    "t_s": round(imp["t_s"], 3),
                    **{f"{b} dB": round(v, 2) for b, v in imp["levels_db"].items()},
                }
            )
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        if not rows:
            return
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="a data/raw/<date> folder")
    parser.add_argument("--threshold-db", type=float, default=THRESHOLD_DB)
    parser.add_argument("--baseline", metavar="TRIAL_ID",
                        help="noise-only trial to measure all trials against "
                             "(default: the latest one before each trial)")
    args = parser.parse_args(argv)
    try:
        trials = analyze_folder(args.folder, args.threshold_db, baseline_trial=args.baseline)
    except ValueError as e:
        parser.error(str(e))
    for t in trials:
        meta = t["meta"]
        if t["result"] is None:
            levels = ", ".join(f"{b} {v:.2f}" for b, v in t["noise_baseline"].items())
            print(f"{meta['trial_id']}: noise only, baseline (counts) {levels}")
            continue
        found = len(t["result"]["impacts"])
        made = meta.get("impacts_made")
        print(
            f"{meta['trial_id']}: {meta.get('distance_m')} m, {found} impacts"
            + (f" ({made} made)" if made is not None else "")
            + f", baseline {t['result']['baseline_source']}"
        )
    summary = summarize(trials)
    heads = "  ".join(f"{b} mean (sd)" for b in REPORTED)
    print(f"\nexcitation  distance  trials  found  made  {heads}")
    for r in summary:
        cells = "  ".join(
            f"{_fmt(r[f'{b} mean dB'])} ({_fmt(r[f'{b} std dB'])})".rjust(len(b) + 11)
            for b in REPORTED
        )
        print(
            f"{r['excitation']:<10}  {_fmt(r['distance_m']):>8}  {r['trials']:>6}  "
            f"{r['impacts_found']:>5}  {_fmt(r['impacts_made']):>4}  {cells}"
        )
    write_csv(args.folder / "impacts_trials.csv", trial_rows(trials))
    write_csv(args.folder / "impacts_summary.csv", summary)
    print(f"\nwrote {args.folder / 'impacts_trials.csv'} and {args.folder / 'impacts_summary.csv'}")


if __name__ == "__main__":
    main()
