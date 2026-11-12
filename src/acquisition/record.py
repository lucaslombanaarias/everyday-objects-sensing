"""Record one trial from the VPU PCM stream to CSV, a JSON sidecar and a .mat file.

Run as ``python -m acquisition.record --port COM3 --trial-id <id> --duration <s>`` plus
metadata flags (``--help`` lists them). The board must run firmware/vpu_pcm_stream; this
switches it to binary mode for the recording and back to stats mode afterwards. Files go
to <out-dir>/<date>/<trial-id>.{csv,json,mat}; data/raw/ is not committed.

The sanity checks printed after each trial are also stored in the sidecar: CRC errors,
counter gaps (lost samples), device DMA overflows, clipped samples, and the sample rate
measured against the host clock versus the nominal rate.
"""

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import numpy as np
import scipy.io

from acquisition.frames import Frame, FrameParser

NOMINAL_FS_HZ = 48000.0
"""Output rate of firmware/vpu_pcm_stream at its default 3.072 MHz PDM clock."""
FS_TOLERANCE = 0.02
"""Largest accepted relative difference between measured and nominal sample rate. The
measurement uses host arrival times, so it includes USB timing jitter."""
INT16_LIMITS = (-32768, 32767)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "raw"
FIRMWARE_DIR = "firmware/vpu_pcm_stream"
META_VERSION = 2
"""Sidecars without meta_version were written before the fields below were added."""
EXCITATIONS = ("drop_stand", "ball_drop", "single_step", "double_step", "walking", "stomp",
               "ice_drop", "water_pour")
UNCOUNTED = ("water_pour",)
"""Excitations that are not a number of impacts (a continuous pour): impacts made is not
required for them and is left null."""
MOUNTS = ("tape", "putty_wax", "loose", "preloaded")


@dataclass
class Capture:
    sample_index: np.ndarray
    """Device sample counter for each sample; jumps where samples were lost."""
    counts: np.ndarray
    """PCM samples, int16; PDM full scale is +-32768."""
    host_time_s: np.ndarray
    """Arrival time of each sample's frame, seconds since the recording started."""
    checks: dict


def assemble(frames: list[Frame], parser: FrameParser, nominal_fs_hz: float) -> Capture:
    gaps = missing = out_of_order = 0
    expected = None
    for f in frames:
        if expected is not None and f.first_sample != expected:
            if f.first_sample > expected:
                gaps += 1
                missing += f.first_sample - expected
            else:
                out_of_order += 1
        expected = f.first_sample + f.samples.size

    if frames:
        index = np.concatenate([f.first_sample + np.arange(f.samples.size) for f in frames])
        counts = np.concatenate([f.samples for f in frames]).astype(np.int16)
        host = np.concatenate([np.full(f.samples.size, f.host_time_s) for f in frames])
    else:
        index = np.zeros(0, dtype=np.int64)
        counts = np.zeros(0, dtype=np.int16)
        host = np.zeros(0)

    measured_fs = None
    if len(frames) >= 2 and frames[-1].host_time_s > frames[0].host_time_s:
        span = frames[-1].first_sample + frames[-1].samples.size - frames[0].first_sample
        measured_fs = span / (frames[-1].host_time_s - frames[0].host_time_s)
    fs_ok = measured_fs is not None and abs(measured_fs / nominal_fs_hz - 1) <= FS_TOLERANCE
    clipped = int(np.count_nonzero((counts == INT16_LIMITS[0]) | (counts == INT16_LIMITS[1])))
    overflows = (frames[-1].ovf - frames[0].ovf) & 0xFFFF if frames else 0
    checks = {
        "frames": len(frames),
        "samples": int(counts.size),
        "crc_errors": parser.crc_errors,
        "skipped_bytes_after_first_frame": parser.skipped_bytes,
        "counter_gaps": gaps,
        "missing_samples": int(missing),
        "out_of_order_frames": out_of_order,
        "device_overflow_events": int(overflows),
        "clipped_samples": clipped,
        "nominal_fs_hz": nominal_fs_hz,
        "measured_fs_hz": measured_fs,
        "fs_within_tolerance": bool(fs_ok),
    }
    checks["ok"] = bool(
        frames
        and fs_ok
        and not (parser.crc_errors or gaps or out_of_order or overflows or clipped)
    )
    return Capture(index, counts, host, checks)


def acquire(ser, duration_s: float, nominal_fs_hz: float = NOMINAL_FS_HZ, now=time.monotonic):
    """Switch the board to binary mode, read frames for duration_s, switch it back."""
    parser = FrameParser()
    frames: list[Frame] = []
    ser.write(b"B")
    t0 = now()
    while (t := now() - t0) < duration_s:
        data = ser.read(65536)
        if data:
            frames += parser.feed(data, t)
    ser.write(b"S")
    return assemble(frames, parser, nominal_fs_hz)


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def repo_state() -> dict:
    status = _git("status", "--porcelain")
    return {
        "repo_commit": _git("rev-parse", "HEAD"),
        "repo_dirty": None if status is None else bool(status),
        # Source files only, so README edits do not count as firmware changes.
        "fw_source_commit": _git(
            "log", "-1", "--format=%H", "--", f"{FIRMWARE_DIR}/*.ino", f"{FIRMWARE_DIR}/*.h"
        ),
    }


def distance_label(d: float) -> str:
    """1 -> '1m', 0.5 -> '0p5m', 2.25 -> '2p25m'."""
    return f"{d:g}".replace(".", "p") + "m"


def next_trial_id(folder: Path, prefix: str, kind: str | None, distance: float | None,
                  also=()) -> str:
    """<prefix>_<distance or 'noise'>_<nn>, numbered after the trials already in folder
    and in the folders in also (e.g. the same day's shared trials, which may come from
    another computer); <prefix>_<nn> when neither kind nor distance is given."""
    if kind == "noise only":
        stem = f"{prefix}_noise"
    elif distance:
        stem = f"{prefix}_{distance_label(distance)}"
    else:
        stem = prefix
    taken = [int(m.group(1)) for d in (folder, *also) for f in Path(d).glob(f"{stem}_*.json")
             if (m := re.fullmatch(re.escape(stem) + r"_(\d+)", f.stem))]
    return f"{stem}_{max(taken, default=0) + 1:02d}"


ESPRESSIF_VID = 0x303A
"""USB vendor ID of the XIAO ESP32-C6's built-in USB serial port (VID:PID 303A:1001)."""


def find_board_port(ports) -> str | None:
    """The board's serial port among serial.tools.list_ports.comports(): the one with
    Espressif's vendor ID (the first, by name, if there are several), else None. Other
    ports (Bluetooth, other USB devices) are never taken for the board, so nothing else
    is sent its commands."""
    espressif = sorted(p.device for p in ports if p.vid == ESPRESSIF_VID)
    return espressif[0] if espressif else None

def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def _for_mat(value):
    """savemat cannot store None; nested dicts become MATLAB structs."""
    if isinstance(value, dict):
        return {k: _for_mat(v) for k, v in value.items()}
    return "" if value is None else value


def first_sample_offset_s(host_time_s: np.ndarray, fs_hz: float) -> float:
    """Host time of the first sample, from the arrival of the first read: the samples of
    that read were all buffered, so the first of them was taken their duration earlier."""
    if not host_time_s.size:
        return 0.0
    first = np.count_nonzero(host_time_s == host_time_s[0])
    return float(host_time_s[0] - first / fs_hz)


def validate_meta(meta: dict) -> list[str]:
    """What is missing or not allowed in a new trial's metadata (empty if it is fine).
    Sidecars from before META_VERSION 2 are not checked; see normalize_meta."""
    problems = []
    mount = (meta.get("sensor_mount") or {}).get("method")
    if mount not in MOUNTS:
        problems.append(f"mount must be one of {', '.join(MOUNTS)} (got {mount!r})")
    if meta.get("noise_only"):
        return problems
    kind = (meta.get("excitation") or {}).get("type")
    if kind not in EXCITATIONS:
        problems.append(f"excitation must be one of {', '.join(EXCITATIONS)} (got {kind!r})")
    if kind in UNCOUNTED:
        return problems
    made = meta.get("impacts_made")
    if not isinstance(made, int) or isinstance(made, bool) or made < 1:
        problems.append("impacts made is required: the number of impacts made, 1 or more")
    return problems


def normalize_meta(meta: dict) -> dict:
    """A sidecar with every current field present: fields added after it was written are
    None. Older values (excitation "footsteps", mount "tape plus weight") are kept as
    they are."""
    out = dict(meta)
    for key in ("meta_version", "floor_material", "distance_m", "impacts_made",
                "excitation_point_id", "baseline_noise_id"):
        out.setdefault(key, None)
    out["excitation"] = {"type": None, "mass_g": None, "drop_height_cm": None, "tip": None,
                         **(out.get("excitation") or {})}
    out["sensor_mount"] = {"method": None, "preload": None, **(out.get("sensor_mount") or {})}
    return out


def load_meta(json_path: Path) -> dict:
    return normalize_meta(json.loads(Path(json_path).read_text(encoding="utf-8")))


def write_trial(capture: Capture, meta: dict, out_dir: Path, overwrite: bool = False) -> dict:
    """Write <out_dir>/<date>/<trial_id>.{csv,json,mat}; returns the paths."""
    folder = Path(out_dir) / meta["date"]
    folder.mkdir(parents=True, exist_ok=True)
    paths = {ext: folder / f"{meta['trial_id']}.{ext}" for ext in ("csv", "json", "mat")}
    existing = [str(p) for p in paths.values() if p.exists()]
    if existing and not overwrite:
        raise FileExistsError(f"trial files already exist: {existing}")

    table = np.column_stack([capture.sample_index, capture.host_time_s, capture.counts])
    np.savetxt(
        paths["csv"],
        table,
        fmt=("%d", "%.6f", "%d"),
        delimiter=",",
        header="sample_index,host_time_s,counts",
        comments="",
    )

    sidecar = {
        **meta,
        "checks": capture.checks,
        "files": {"csv": paths["csv"].name, "mat": paths["mat"].name},
        "software": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "pyserial": _version("pyserial"),
        },
    }
    scipy.io.savemat(
        paths["mat"],
        {
            "counts": capture.counts.reshape(-1, 1),
            "sample_index": capture.sample_index.astype(np.float64).reshape(-1, 1),
            "host_time_s": capture.host_time_s.reshape(-1, 1),
            "fs_hz": meta["fs_hz"],
            "meta": _for_mat({k: v for k, v in sidecar.items() if k != "checks"}),
            "checks": _for_mat(capture.checks),
        },
        long_field_names=True,
        do_compression=True,
    )
    # The sidecar goes last: once it exists, the trial's other files are complete.
    paths["json"].write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    return paths


def trial_files(json_path: Path) -> list[Path]:
    """A trial's files that exist, sidecar first."""
    json_path = Path(json_path)
    return [p for p in (json_path.with_suffix(ext) for ext in (".json", ".mat", ".csv"))
            if p.exists()]


def trash_trial(json_path: Path) -> str:
    """Move a trial's .json, .mat and .csv to the Windows Recycle Bin, so they can be
    restored. Elsewhere they move to a trash/ folder next to them, which the analysis does
    not read. The sidecar goes first, so a half-finished move never leaves a trial that
    looks complete. Returns where the files went; raises OSError if any remain."""
    files = trial_files(json_path)
    if not files:
        raise FileNotFoundError(f"no files for trial {json_path}")
    if sys.platform == "win32":
        quoted = ", ".join("'" + str(p.resolve()).replace("'", "''") + "'" for p in files)
        script = (
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            f"foreach ($f in @({quoted})) {{ "
            "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
            "$f, 'OnlyErrorDialogs', 'SendToRecycleBin') }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, check=False,
        )
        where = "the Recycle Bin"
    else:
        trash = Path(json_path).parent / "trash"
        trash.mkdir(exist_ok=True)
        for p in files:
            p.replace(trash / p.name)
        where = str(trash)
    left = [str(p) for p in files if p.exists()]
    if left:
        raise OSError(f"could not move to {where}: {left}")
    return where


EDITABLE = ("notes", "noise_only", "excitation.type", "impacts_made", "excitation_point_id",
            "distance_m", "excitation.drop_height_cm", "excitation.tip", "floor_material",
            "sensor_mount.method", "baseline_noise_id", "operator")
"""Sidecar fields that can be changed after recording (edit_trial); a dot is a nested
field, e.g. excitation.type."""


def get_field(meta: dict, key: str):
    for part in key.split("."):
        meta = (meta or {}).get(part)
    return meta


def apply_fields(meta: dict, fields: dict) -> dict:
    """Set fields named as in EDITABLE (e.g. {"excitation.type": "stomp"}) in meta."""
    for key, value in fields.items():
        *parents, last = key.split(".")
        target = meta
        for part in parents:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[last] = value
    return meta


def _add_edit_note(sidecar: dict, note: str, today: datetime.date | None) -> None:
    note = f"{(today or datetime.date.today()).isoformat()}: {note}"
    sidecar["edit_note"] = "; ".join(filter(None, [sidecar.get("edit_note"), note]))


def _write_mat_meta(mat_path: Path, sidecar: dict) -> None:
    """Replace the metadata stored in a trial's .mat with the sidecar's, keeping its data."""
    data = {k: v for k, v in scipy.io.loadmat(mat_path).items() if not k.startswith("__")}
    data["meta"] = _for_mat({k: v for k, v in sidecar.items() if k != "checks"})
    if "checks" in sidecar:
        data["checks"] = _for_mat(sidecar["checks"])
    tmp = mat_path.with_name(mat_path.name + ".tmp")
    scipy.io.savemat(tmp, data, appendmat=False, long_field_names=True, do_compression=True)
    os.replace(tmp, mat_path)


def _write_json(path: Path, sidecar: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def edit_trial(json_path: Path, fields: dict, today: datetime.date | None = None) -> list[str]:
    """Change fields of a saved trial (see EDITABLE) in its sidecar and in the copy of the
    metadata in its .mat, and add the old values to edit_note. Returns the fields that
    changed; nothing is written when none did."""
    json_path = Path(json_path)
    bad = [k for k in fields if k not in EDITABLE]
    if bad:
        raise ValueError(f"cannot edit {', '.join(bad)}")
    sidecar = json.loads(json_path.read_text(encoding="utf-8"))
    changed = [(k, get_field(sidecar, k)) for k, v in fields.items() if get_field(sidecar, k) != v]
    if not changed:
        return []
    apply_fields(sidecar, {k: fields[k] for k, _ in changed})
    _add_edit_note(sidecar, "edited in the dashboard; before: " + ", ".join(
        f"{k} {old!r}" for k, old in changed), today)
    mat_path = json_path.with_name((sidecar.get("files") or {}).get("mat", json_path.stem + ".mat"))
    if mat_path.exists():
        _write_mat_meta(mat_path, sidecar)
    _write_json(json_path, sidecar)
    return [k for k, _ in changed]


TRIAL_ID = re.compile(r"\w[\w.-]*")
"""A trial ID that is also a usable file name."""


def rename_trial(json_path: Path, new_id: str, today: datetime.date | None = None) -> Path:
    """Rename a saved trial: its .json, .mat and .csv, its trial_id and file names in the
    sidecar and in the .mat, with the old name added to edit_note. Other trials in its
    folder that name it as their baseline_noise_id (it is a noise-only trial) are changed
    to the new name too, with edit_trial. Returns the new sidecar path.

    Raises ValueError for a name that cannot be a file name, FileExistsError when the name
    is taken in the folder, and OSError when a file cannot be moved (e.g. the .mat is open
    in MATLAB); then nothing is renamed."""
    json_path = Path(json_path)
    folder = json_path.parent
    if not TRIAL_ID.fullmatch(new_id):
        raise ValueError(f"{new_id!r} cannot be a trial ID: use letters, digits, _ . and -")
    sidecar = json.loads(json_path.read_text(encoding="utf-8"))
    old = sidecar["trial_id"]
    if new_id == old:
        return json_path
    taken = [p.name for p in (folder / f"{new_id}.{ext}" for ext in ("json", "mat", "csv"))
             if p.exists()]
    if taken:
        raise FileExistsError(f"{', '.join(taken)} already in {folder}")
    files = sidecar.get("files") or {}
    moved = []
    try:
        for ext in ("csv", "mat"):
            src = folder / files.get(ext, f"{old}.{ext}")
            if src.exists():
                dst = folder / f"{new_id}.{ext}"
                os.rename(src, dst)
                moved.append((src, dst))
        sidecar["trial_id"] = new_id
        sidecar["files"] = {**files, "csv": f"{new_id}.csv", "mat": f"{new_id}.mat"}
        _add_edit_note(sidecar, f"renamed from {old} to {new_id} in the dashboard", today)
        if (folder / f"{new_id}.mat").exists():
            _write_mat_meta(folder / f"{new_id}.mat", sidecar)
    except OSError:
        for src, dst in reversed(moved):
            os.rename(dst, src)
        raise
    # The sidecar goes last, as in write_trial: once it exists, the other files are there.
    new_json = folder / f"{new_id}.json"
    _write_json(new_json, sidecar)
    json_path.unlink()
    for path in folder.glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and "checks" in meta and meta.get("baseline_noise_id") == old:
            edit_trial(path, {"baseline_noise_id": new_id}, today)
    return new_json


def list_recordings(out_dir: Path = DEFAULT_OUT_DIR, controlled: Path | None = None) -> list[dict]:
    """Every trial saved under out_dir/<day>/, newest first: its sidecar path and metadata,
    and whether it is shared, i.e. in controlled/raw/<day>/ (matched by start time, so a
    copy renamed after sharing counts): "yes", "changed" when the local sidecar differs
    from the shared one, "as <id>" when it was shared under another name, else "".
    shared_json is the shared copy's sidecar, or None."""
    controlled = REPO_ROOT / "data" / "controlled" if controlled is None else Path(controlled)
    out = []
    for day in sorted(p for p in Path(out_dir).glob("*") if p.is_dir()):
        shared = {}
        for p in (controlled / "raw" / day.name).glob("*.json"):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            shared[m.get("start_time")] = (m, p)
        for path in day.glob("*.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not (isinstance(meta, dict) and "checks" in meta and "files" in meta):
                continue  # not a trial sidecar
            copy, copy_path = shared.get(meta.get("start_time"), (None, None))
            if copy is None:
                status = ""
            elif copy.get("trial_id") != meta.get("trial_id"):
                status = f"as {copy.get('trial_id')}"
            else:
                status = "yes" if copy == meta else "changed"
            out.append({"json": path, "day": day.name, "meta": normalize_meta(meta),
                        "shared": status, "shared_json": copy_path})
    out.sort(key=lambda r: (r["day"], r["meta"].get("start_time") or ""), reverse=True)
    return out


def build_meta(args: argparse.Namespace, start: datetime.datetime) -> dict:
    return {
        "meta_version": META_VERSION,
        "trial_id": args.trial_id,
        "date": start.date().isoformat(),
        "start_time": start.isoformat(timespec="milliseconds"),
        "noise_only": args.noise_only,
        "operator": args.operator,
        "floor_material": args.floor_material,
        "floor_location": args.floor_location,
        "distance_m": args.distance_m,
        "excitation": {
            "type": "none" if args.noise_only else args.excitation_type,
            "mass_g": args.mass_g,
            "drop_height_cm": args.drop_height_cm,
            "tip": args.tip,
        },
        "excitation_point_id": args.excitation_point,
        "impacts_made": 0 if args.noise_only else args.impacts,
        "sensor_mount": {"method": args.mount_method, "preload": args.preload},
        "baseline_noise_id": args.baseline_noise_id,
        "sensor_serial": args.sensor_serial,
        "ambient_conditions": args.ambient,
        "notes": args.notes,
        "fw_commit": args.fw_commit,
        **repo_state(),
        "fs_hz": args.fs_hz,
        "port": args.port,
        "requested_duration_s": args.duration,
    }


def print_checks(checks: dict) -> None:
    fs = checks["measured_fs_hz"]
    fs_text = "n/a" if fs is None else f"{fs:.1f}"
    print(
        f"frames {checks['frames']}, samples {checks['samples']}, "
        f"measured fs {fs_text} Hz (nominal {checks['nominal_fs_hz']:g})"
    )
    for key in (
        "crc_errors",
        "counter_gaps",
        "missing_samples",
        "out_of_order_frames",
        "device_overflow_events",
        "clipped_samples",
    ):
        flag = "" if checks[key] == 0 else "   <-- check"
        print(f"  {key}: {checks[key]}{flag}")
    if not checks["fs_within_tolerance"]:
        print(f"  sample rate differs from nominal by more than {FS_TOLERANCE:.0%}   <-- check")
    print("checks OK" if checks["ok"] else "CHECKS FAILED: see above")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", required=True, help="serial port, e.g. COM3")
    p.add_argument("--trial-id", required=True)
    p.add_argument("--duration", type=float, required=True, help="seconds")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--fs-hz", type=float, default=NOMINAL_FS_HZ,
                   help="nominal output rate of the flashed firmware build")
    p.add_argument("--noise-only", action="store_true",
                   help="ambient baseline recording with no excitation")
    p.add_argument("--operator")
    p.add_argument("--floor-material")
    p.add_argument("--floor-location")
    p.add_argument("--distance-m", type=float,
                   help="sensor radius: impact point to sensor center, in m")
    p.add_argument("--excitation-type", help=f"one of {', '.join(EXCITATIONS)}")
    p.add_argument("--excitation-point", help="ID of the fixed impact point on this floor")
    p.add_argument("--mass-g", type=float)
    p.add_argument("--drop-height-cm", type=float)
    p.add_argument("--tip")
    p.add_argument("--impacts", type=int,
                   help="number of footsteps, stomps or drops made during the trial "
                        "(required unless --noise-only or a water_pour)")
    p.add_argument("--mount-method", help=f"one of {', '.join(MOUNTS)} (required)")
    p.add_argument("--preload", help="e.g. mass on top of the sensor")
    p.add_argument("--sensor-serial")
    p.add_argument("--baseline-noise-id",
                   help="noise-only trial recorded as the baseline for this block")
    p.add_argument("--ambient", help="people, HVAC, other noise sources present")
    p.add_argument("--notes")
    p.add_argument("--fw-commit", help="commit the flashed firmware was built from")
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args(argv)
    excitation = (args.excitation_type, args.mass_g, args.drop_height_cm, args.tip, args.impacts)
    if args.noise_only and any(v is not None for v in excitation):
        p.error("--noise-only cannot be combined with excitation flags")
    if args.duration <= 0:
        p.error("--duration must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    import serial

    args = parse_args(argv)
    problems = validate_meta(build_meta(args, datetime.datetime.now().astimezone()))
    if problems:
        print("not recording:\n  " + "\n  ".join(problems))
        return 2
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = args.port, 115200, 0.05
    ser.dtr, ser.rts = True, False  # RTS high would hold the ESP32-C6 in reset
    try:
        ser.open()
    except serial.SerialException as e:
        print(f"cannot open {args.port}: {e}\nClose the Arduino Serial Monitor and dashboards.")
        return 2
    start = datetime.datetime.now().astimezone()
    print(f"recording {args.trial_id} for {args.duration:g} s from {args.port} ...")
    try:
        ser.reset_input_buffer()
        capture = acquire(ser, args.duration, args.fs_hz)
    finally:
        ser.close()
    start += datetime.timedelta(seconds=first_sample_offset_s(capture.host_time_s, args.fs_hz))
    if not capture.checks["frames"]:
        print("no frames received: is firmware/vpu_pcm_stream flashed and nothing else "
              "using the port?")
        return 1
    paths = write_trial(capture, build_meta(args, start), args.out_dir, args.overwrite)
    print_checks(capture.checks)
    if args.fw_commit is None:
        print("note: --fw-commit not given; the sidecar records the latest repo commit of "
              f"{FIRMWARE_DIR} instead, which may differ from what is flashed")
    for path in paths.values():
        print(f"wrote {path}")
    return 0 if capture.checks["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
