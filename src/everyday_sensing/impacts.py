"""Find impacts (footsteps, stomps, drops) in recorded trials and summarize them by distance.

Run as ``python -m everyday_sensing.impacts data/raw/<date>`` to print one row per trial
and one per distance. The outputs go to data/analysis/<date>/ (see analysis_folder), never
into data/raw. The PCM dashboard (tools/dashboard/vpu_pcm_dashboard.py) runs the same
analysis after every recording.

Detection criterion (DETECTION, version det-v1): an impact is a local maximum of the RMS
over a 5 ms detection window of the signal high-passed at 100 Hz (4th-order Butterworth,
causal), evaluated 1000 times per second, that is at least 14 dB above the noise baseline.
Of peaks closer together than 0.25 s only the highest is kept. Footsteps on the tile floor
arrive as clicks a few ms long; a 50 ms window (used before 2026-10-14 23:10) averaged most
of them away at 2 m.

The baseline is the median detection level of the most recent noise-only trial recorded at
or before the trial in the same folder, skipping its first 1 s (any noise-only trial if none
precedes it, else the quietest 2 s of the trial itself); another noise-only trial can be
chosen instead.

Each impact is also reported in BANDS, from the 50 ms RMS (as in the stomp and first
footstep checks), so levels stay comparable with those numbers. These are features only:
they are not used for detection.
"""

import argparse
import csv
import dataclasses
import datetime
import json
import math
from pathlib import Path

import numpy as np
import scipy.io
from scipy import signal

from acquisition.record import load_meta
from everyday_sensing.events import BandEnvelope


@dataclasses.dataclass(frozen=True)
class Detection:
    """The detection criterion. Any change to it is a new version."""

    version: str = "det-v1"
    highpass_hz: float = 100.0
    filter_order: int = 4
    filter: str = "Butterworth high-pass, causal, started settled at the first sample"
    window_s: float = 0.005
    """Detection window: the RMS is taken over this many seconds."""
    rate_hz: float = 1000.0
    """How often the detection level is evaluated (one RMS value per 1 ms)."""
    threshold_db: float = 14.0
    """Set from the trials of 2026-10-14: at 14 dB the detector fired 1.3 to 4.1 times per
    minute on the three noise-only recordings, and found 7 of 9 firm steps in fs_2m_01 (16
    dB: 3 of 9) and 9 of 9 stomping steps in fs_2m_03 and fs_2m_04."""
    min_gap_s: float = 0.25
    """Of peaks closer than this only the highest is kept; the 2 m steps on 2026-10-14 were
    0.5 s apart."""
    baseline_skip_s: float = 1.0
    """Start of a noise-only recording left out of its baseline (filter start-up)."""
    quiet_window_s: float = 2.0
    """Without a noise-only trial, the baseline is the quietest stretch this long."""

    def describe(self) -> str:
        return (
            f"{self.version}: local maxima of the RMS over a {self.window_s * 1000:g} ms "
            f"detection window of the signal high-passed at {self.highpass_hz:g} Hz "
            f"({self.filter_order}th-order {self.filter}), evaluated {self.rate_hz:g} times "
            f"per second, at least {self.threshold_db:g} dB above the noise baseline; of "
            f"peaks closer than {self.min_gap_s:g} s only the highest is kept. Baseline: "
            f"median detection level of the latest noise-only trial at or before the trial "
            f"(any noise-only trial if none precedes it), skipping its first "
            f"{self.baseline_skip_s:g} s; else the quietest {self.quiet_window_s:g} s of "
            f"the trial."
        )

    def as_dict(self) -> dict:
        return {**dataclasses.asdict(self), "criterion": self.describe()}


DETECTION = Detection()
"""det-v1, the frozen criterion. All default outputs use it."""


def detection_for(threshold_db: float | None = None) -> Detection:
    """DETECTION, or a copy with another threshold whose version says it was modified."""
    if threshold_db is None or threshold_db == DETECTION.threshold_db:
        return DETECTION
    return dataclasses.replace(
        DETECTION, threshold_db=threshold_db,
        version=f"{DETECTION.version} (modified: threshold {threshold_db:g} dB)")


DETECT = "above 100 Hz (5 ms)"
DETECT_BAND = (DETECTION.highpass_hz, None)
DETECT_WINDOW_S = DETECTION.window_s
DETECT_RATE_HZ = DETECTION.rate_hz
BANDS = {"100-500 Hz": (100.0, 500.0), "500-2000 Hz": (500.0, 2000.0), "2-8 kHz": (2000.0, 8000.0)}
"""50 ms band levels reported per impact as features; not used for detection."""
BAND_WINDOW_S = 0.05
BAND_RATE_HZ = 200.0
REPORTED = (DETECT, *BANDS)
"""Levels reported per impact: the detection level first, then the 50 ms band levels."""
THRESHOLD_DB = DETECTION.threshold_db
MIN_GAP_S = DETECTION.min_gap_s
EDGE_S = 1.0
"""Events this close to the start or end of a recording are flagged "edge" (walking in or
out, the operator's hand); they are flagged only, never dropped."""
TAGS = ("true_impact", "walk_in_out", "other_noise", "unknown")
UNREVIEWED = "unreviewed"
EVENT_COLUMNS = ("event", "t_s", "timestamp", "peak_db", "flag", "annotation")
PEAK_WINDOW_S = 0.05
"""Half-width of the window in which each band's level is read around a detected peak."""
QUIET_WINDOW_S = DETECTION.quiet_window_s
SKIP_S = DETECTION.baseline_skip_s
"""Start of a recording left out of its noise baseline (filter start-up)."""


def envelopes(x, fs) -> dict:
    """{name: (rate, RMS envelope in counts)} for the detection level and each band."""
    x = np.asarray(x, dtype=np.float64)
    specs = {DETECT: (DETECT_BAND, DETECT_WINDOW_S, DETECT_RATE_HZ, DETECTION.filter_order)}
    specs.update({name: (band, BAND_WINDOW_S, BAND_RATE_HZ, 4) for name, band in BANDS.items()})
    out = {}
    for name, (band, window, rate, order) in specs.items():
        env = BandEnvelope(fs, band, window_s=window, out_rate_hz=rate, order=order)
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
    return {"baseline": baseline, "baseline_source": source, "impacts": impacts,
            "duration_s": np.asarray(x).size / fs}


def detection_traces(x, fs, baseline: dict | None = None,
                     detection: Detection = DETECTION) -> dict:
    """What the detector sees, for plotting one trial: the high-passed signal (counts), its
    RMS over the detection window (counts), that level in dB above the baseline, and the
    threshold in counts. Same filter, window and time convention as find_impacts, so the
    events it reports sit on the peaks of the RMS curve."""
    x = np.asarray(x, dtype=np.float64)
    env = BandEnvelope(fs, (detection.highpass_hz, None), window_s=detection.window_s,
                       out_rate_hz=detection.rate_hz, order=detection.filter_order)
    rms = env.process(x)
    hp = signal.sosfilt(env.sos, x, zi=signal.sosfilt_zi(env.sos) * x[0])[0] if x.size else x
    if baseline is None:
        baseline = quiet_baseline({DETECT: (env.rate, rms)})
    base = max(baseline[DETECT], np.finfo(np.float64).tiny)
    return {
        "t_s": np.arange(x.size) / fs,
        "highpassed": hp,
        "rms_t_s": (np.arange(rms.size) + 1) / env.rate,
        "rms": rms,
        "level_db": 20 * np.log10(np.maximum(rms, np.finfo(np.float64).tiny) / base),
        "baseline": base,
        "threshold_counts": base * 10 ** (detection.threshold_db / 20),
    }


# --------------------------------------------------------------------------- folders


def load_trial(json_path: Path) -> tuple[dict, np.ndarray, float]:
    meta = load_meta(json_path)
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


def _when(meta: dict) -> datetime.datetime:
    """Start time for ordering trials; sidecars store it to the second (before meta_version
    2) or to the millisecond, so the strings are not compared directly."""
    try:
        when = datetime.datetime.fromisoformat(meta["start_time"])
        return when if when.tzinfo else when.astimezone()
    except (KeyError, TypeError, ValueError):
        return datetime.datetime.min.replace(tzinfo=datetime.UTC)


def trial_jsons(folder: Path) -> list[Path]:
    """Sidecars written by acquisition.record in folder, oldest first."""
    found = []
    for path in Path(folder).glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict) and "checks" in meta and "files" in meta:
            found.append((_when(meta), path))
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
        meta = load_meta(path)
        if is_noise(meta):
            key = ("noise", str(path), path.stat().st_mtime)
            if key not in cache:
                loaded = _try_load(path)
                if loaded is None:
                    continue
                cache[key] = noise_baseline(loaded[1], loaded[2])
            noises.append((_when(meta), meta["trial_id"], cache[key]))
            trials.append({"meta": meta, "noise_baseline": cache[key], "result": None})
    chosen = None
    if baseline_trial is not None:
        chosen = next((n for n in noises if n[1] == baseline_trial), None)
        if chosen is None:
            raise ValueError(f"no noise-only trial named {baseline_trial!r} in {folder}")
    for path in paths:
        meta = load_meta(path)
        if is_noise(meta):
            continue
        if chosen is not None:
            ref = chosen
        else:
            before = [n for n in noises if n[0] <= _when(meta)]
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
    trials.sort(key=lambda t: _when(t["meta"]))
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


# --------------------------------------------------------------------------- outputs


def analysis_folder(folder, detection: Detection = DETECTION, baseline_trial=None) -> Path:
    """Where the outputs for a folder of trials go: data/analysis/<date>/ for
    data/raw/<date>/ (else an analysis/ folder inside it). Runs with a modified criterion
    or a chosen baseline go to a subfolder named after the change, so they never
    overwrite the det-v1 outputs or the tags in them."""
    folder = Path(folder)
    if folder.parent.name == "raw":
        out = folder.parent.parent / "analysis" / folder.name
    else:
        out = folder / "analysis"
    variant = []
    if detection != DETECTION:
        variant.append(f"threshold_{detection.threshold_db:g}dB")
    if baseline_trial:
        variant.append(f"baseline_{baseline_trial}")
    return out / "_".join(variant) if variant else out


def _label(key: str) -> str:
    """CSV column name: the 50 ms band levels are marked as features."""
    for band in BANDS:
        if key.startswith(band + " "):
            return f"feature {band} 50 ms (not detection){key[len(band):]}"
    if key.startswith(DETECT + " "):
        return f"detection {DETECT}{key[len(DETECT):]}"
    return key


def _versioned(rows: list[dict], detection: Detection) -> list[dict]:
    config = json.dumps(dataclasses.asdict(detection), separators=(",", ":"))
    return [{**{_label(k): v for k, v in r.items()},
             "detection_version": detection.version, "detection_config": config}
            for r in rows]


def event_rows(t: dict) -> list[dict]:
    """One row per detected event, for events_<trial>.csv. t_s is from the first sample of
    the recording and falls inside the 5 ms detection window at the event's peak (1 ms
    after the window starts); timestamp is the trial's start_time plus t_s. Sidecars
    before meta_version 2 store start_time to the second, taken when recording was
    started, so their timestamps are good to about 1 s; later ones to the first sample,
    within a few ms."""
    meta, result = t["meta"], t["result"]
    try:
        start = datetime.datetime.fromisoformat(meta["start_time"])
    except (KeyError, TypeError, ValueError):
        start = None
    rows = []
    for k, imp in enumerate(result["impacts"], 1):
        when = imp["t_s"]
        edge = when < EDGE_S or when > result["duration_s"] - EDGE_S
        rows.append({
            "event": k,
            "t_s": round(when, 3),
            "timestamp": "" if start is None else (
                start + datetime.timedelta(seconds=when)).isoformat(timespec="milliseconds"),
            "peak_db": round(imp["levels_db"][DETECT], 2),
            "flag": "edge" if edge else "",
            "annotation": UNREVIEWED,
        })
    return rows


def read_events(path: Path) -> list[dict]:
    try:
        with Path(path).open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _write_events(path: Path, rows: list[dict]) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_events(path: Path, rows: list[dict]) -> list[dict]:
    """Write events_<trial>.csv, keeping the annotation of each event already in the file
    at the same time (to 1 ms). Returns the rows written."""
    old = {round(float(r["t_s"]), 3): r.get("annotation") or UNREVIEWED
           for r in read_events(path)}
    rows = [{**r, "annotation": old.get(r["t_s"], r["annotation"])} for r in rows]
    _write_events(path, rows)
    return rows


def save_tags(path: Path, tags: dict[int, str]) -> list[dict]:
    """Set the annotation of events (by event number) in events_<trial>.csv."""
    for tag in tags.values():
        if tag not in (*TAGS, UNREVIEWED):
            raise ValueError(f"tag must be one of {', '.join(TAGS)} (got {tag!r})")
    rows = read_events(path)
    for r in rows:
        r["annotation"] = tags.get(int(r["event"]), r["annotation"])
    _write_events(path, rows)
    return rows


def count_row(t: dict, events: list[dict]) -> dict:
    """Made against found for one trial, with the edge events and the review tags."""
    meta, result = t["meta"], t["result"]
    made, found = meta.get("impacts_made"), len(result["impacts"])
    recorded = meta.get("baseline_noise_id")
    return {
        "trial_id": meta["trial_id"],
        "excitation": meta["excitation"]["type"],
        "excitation_point_id": meta.get("excitation_point_id"),
        "distance_m": meta.get("distance_m"),
        "mount": meta["sensor_mount"]["method"],
        "impacts_made": made,
        "impacts_found": found,
        "difference": None if made is None else found - made,
        "edge_events": sum(e["flag"] == "edge" for e in events),
        **{tag: sum(e["annotation"] == tag for e in events) for tag in (*TAGS, UNREVIEWED)},
        "baseline_used": result["baseline_source"],
        "baseline_recorded": recorded,
        "baseline_mismatch": bool(recorded) and recorded != result["baseline_source"],
    }


def trial_analysis(t: dict, detection: Detection = DETECTION, events=None) -> dict:
    """The per-trial analysis record, <trial>.analysis.json."""
    meta, result = t["meta"], t["result"]
    out = {
        "trial_id": meta["trial_id"],
        "analyzed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "detection": detection.as_dict(),
    }
    if result is None:
        out["noise_baseline_counts"] = t["noise_baseline"]
        return out
    events = event_rows(t) if events is None else events
    out.update({
        **count_row(t, events),
        "baseline_counts": result["baseline"],
        "duration_s": result["duration_s"],
        "edge_s": EDGE_S,
        "events": events,
    })
    return out


def write_outputs(trials: list[dict], out: Path, detection: Detection = DETECTION) -> Path:
    """impacts_trials.csv, impacts_summary.csv, impacts_counts.csv, and per trial
    events_<trial>.csv (keeping any annotations already in it) and <trial>.analysis.json,
    in out."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "impacts_trials.csv", _versioned(trial_rows(trials), detection))
    write_csv(out / "impacts_summary.csv", _versioned(summarize(trials), detection))
    counts = []
    for t in trials:
        trial_id = t["meta"]["trial_id"]
        events = None
        if t["result"] is not None:
            events = write_events(out / f"events_{trial_id}.csv", event_rows(t))
            counts.append(count_row(t, events))
        record = trial_analysis(t, detection, events)
        (out / f"{trial_id}.analysis.json").write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8")
    write_csv(out / "impacts_counts.csv", _versioned(counts, detection))
    return out


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="a data/raw/<date> folder")
    parser.add_argument("--threshold-db", type=float, default=THRESHOLD_DB,
                        help="change the det-v1 threshold; the outputs then go to a "
                             "subfolder and are labeled as modified")
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
        row = count_row(t, event_rows(t))
        print(
            f"{meta['trial_id']}: {meta.get('distance_m')} m, {row['impacts_found']} found"
            + ("" if row["impacts_made"] is None else
               f", {row['impacts_made']} made, difference {row['difference']:+d}")
            + (f", {row['edge_events']} at the edges" if row["edge_events"] else "")
            + f", baseline {row['baseline_used']}"
            + (f" (recorded: {row['baseline_recorded']})" if row["baseline_mismatch"] else "")
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
    detection = detection_for(args.threshold_db)
    out = write_outputs(trials, analysis_folder(args.folder, detection, args.baseline), detection)
    print(f"\ndetection {detection.describe()}")
    print(f"wrote impacts_trials.csv, impacts_summary.csv, impacts_counts.csv, "
          f"events_<trial>.csv and <trial>.analysis.json to {out}")


if __name__ == "__main__":
    main()
