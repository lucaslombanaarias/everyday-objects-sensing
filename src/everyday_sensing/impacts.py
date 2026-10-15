"""Find impacts (footsteps, stomps, drops) in recorded trials and summarize them by distance.

Run as ``python -m everyday_sensing.impacts data/raw/<date>`` to print one row per trial
and one per distance, and write impacts_trials.csv and impacts_summary.csv into that
folder. The PCM dashboard (tools/dashboard/vpu_pcm_dashboard.py) runs the same analysis
after every recording.

Per trial: the 50 ms RMS envelope in each band of BANDS, in dB above a noise baseline.
The baseline is the median envelope of the most recent noise-only trial recorded at or
before the trial in the same folder (any noise-only trial if none precedes it, else the
quietest 2 s of the trial itself). An impact is a peak of the larger of the DETECT_BANDS
levels that reaches THRESHOLD_DB, stands PROMINENCE_DB above its surroundings, and is at
least MIN_GAP_S after the previous one.
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

BANDS = {"100-500 Hz": (100.0, 500.0), "500-2000 Hz": (500.0, 2000.0), "2-8 kHz": (2000.0, 8000.0)}
DETECT_BANDS = ("100-500 Hz", "500-2000 Hz")
"""2-8 kHz is reported but not used for detection: in the first footstep trials on tile
(2026-10-14), walking to the start mark gave 8 to 16 dB peaks in 2-8 kHz with under 6 dB
in both lower bands, i.e. shoe clicks rather than floor vibration."""
THRESHOLD_DB = 10.0
"""In the noise-only recording of 2026-10-14 the 100-500 Hz envelope stayed within 7.9 dB
of its median 99% of the time (500-2000 Hz: 5.4 dB)."""
PROMINENCE_DB = 6.0
MIN_GAP_S = 0.3
PEAK_WINDOW_S = 0.05
"""Half-width of the window in which each band's level is read around a detected peak."""
QUIET_WINDOW_S = 2.0
SKIP_S = 1.0
"""Start of a noise recording left out of its baseline (filter start-up)."""
OUT_RATE_HZ = 200.0


def band_envelopes(x, fs) -> tuple[float, dict]:
    """Envelope rate and the 50 ms RMS envelope (counts) of x in each band."""
    envs = {name: BandEnvelope(fs, band, out_rate_hz=OUT_RATE_HZ) for name, band in BANDS.items()}
    x = np.asarray(x, dtype=np.float64)
    out = {name: env.process(x) for name, env in envs.items()}
    return next(iter(envs.values())).rate, out


def noise_baseline(x, fs) -> dict:
    """Median envelope per band of a noise-only recording, skipping its first SKIP_S."""
    rate, envs = band_envelopes(x, fs)
    skip = int(SKIP_S * rate)
    return {name: float(np.median(e[skip:] if e.size > skip else e)) for name, e in envs.items()}


def quiet_baseline(envs: dict, rate: float) -> dict:
    """Per band, the lowest median over QUIET_WINDOW_S windows (the quietest stretch)."""
    w = max(1, int(QUIET_WINDOW_S * rate))
    hop = max(1, w // 8)
    out = {}
    for name, e in envs.items():
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
    its time and its level (dB above baseline) in each band."""
    rate, envs = band_envelopes(x, fs)
    source = "given"
    if baseline is None:
        baseline, source = quiet_baseline(envs, rate), "quietest 2 s of the trial"
    tiny = np.finfo(np.float64).tiny
    levels = {
        name: 20 * np.log10(np.maximum(e, tiny) / max(baseline[name], tiny))
        for name, e in envs.items()
    }
    detect = np.max(np.vstack([levels[b] for b in DETECT_BANDS]), axis=0)
    peaks, _ = signal.find_peaks(
        detect,
        height=threshold_db,
        distance=max(1, int(min_gap_s * rate)),
        prominence=PROMINENCE_DB,
    )
    half = max(1, int(PEAK_WINDOW_S * rate))
    impacts = []
    for p in peaks:
        window = slice(max(0, p - half), p + half + 1)
        impacts.append(
            {
                "t_s": float((p + 1) / rate),
                "levels_db": {name: float(lv[window].max()) for name, lv in levels.items()},
            }
        )
    return {"baseline": baseline, "baseline_source": source, "impacts": impacts}


# --------------------------------------------------------------------------- folders


def load_trial(json_path: Path) -> tuple[dict, np.ndarray, float]:
    meta = json.loads(Path(json_path).read_text(encoding="utf-8"))
    mat = scipy.io.loadmat(Path(json_path).with_name(meta["files"]["mat"]))
    return meta, mat["counts"].ravel(), float(np.squeeze(mat["fs_hz"]))


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


def analyze_folder(folder, threshold_db: float = THRESHOLD_DB, cache: dict | None = None):
    """Analyze every trial in folder. Returns one dict per trial, oldest first: "meta" (the
    sidecar) and "result" (find_impacts output, None for noise-only trials). cache keeps
    results keyed by file, modification time, threshold and baseline trial, so repeated
    calls only analyze what changed."""
    cache = {} if cache is None else cache
    paths = trial_jsons(folder)
    noises = []
    trials = []
    for path in paths:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if is_noise(meta):
            key = ("noise", str(path), path.stat().st_mtime)
            if key not in cache:
                _, counts, fs = load_trial(path)
                cache[key] = noise_baseline(counts, fs)
            noises.append((meta.get("start_time", ""), meta["trial_id"], cache[key]))
            trials.append({"meta": meta, "noise_baseline": cache[key], "result": None})
    for path in paths:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if is_noise(meta):
            continue
        before = [n for n in noises if n[0] <= meta.get("start_time", "")]
        ref = before[-1] if before else (noises[0] if noises else None)
        key = (str(path), path.stat().st_mtime, threshold_db, ref[1] if ref else None)
        if key not in cache:
            _, counts, fs = load_trial(path)
            result = find_impacts(counts, fs, ref[2] if ref else None, threshold_db)
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
        for band in BANDS:
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
    args = parser.parse_args(argv)
    trials = analyze_folder(args.folder, args.threshold_db)
    for t in trials:
        meta = t["meta"]
        if t["result"] is None:
            print(f"{meta['trial_id']}: noise only")
            continue
        found = len(t["result"]["impacts"])
        made = meta.get("impacts_made")
        print(
            f"{meta['trial_id']}: {meta.get('distance_m')} m, {found} impacts"
            + (f" ({made} made)" if made is not None else "")
            + f", baseline {t['result']['baseline_source']}"
        )
    summary = summarize(trials)
    heads = "  ".join(f"{b} mean (sd)" for b in BANDS)
    print(f"\nexcitation  distance  trials  found  made  {heads}")
    for r in summary:
        cells = "  ".join(
            f"{_fmt(r[f'{b} mean dB'])} ({_fmt(r[f'{b} std dB'])})".rjust(len(b) + 11)
            for b in BANDS
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
