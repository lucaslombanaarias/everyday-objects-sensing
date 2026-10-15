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


def next_trial_id(folder: Path, prefix: str, kind: str, distance: float) -> str:
    """<prefix>_<distance or 'noise'>_<nn>, numbered after the trials already in folder."""
    stem = f"{prefix}_{'noise' if kind == 'noise only' else distance_label(distance)}"
    taken = [int(m.group(1)) for f in Path(folder).glob(f"{stem}_*.json")
             if (m := re.fullmatch(re.escape(stem) + r"_(\d+)", f.stem))]
    return f"{stem}_{max(taken, default=0) + 1:02d}"

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
    paths["json"].write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")

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
    return paths


def build_meta(args: argparse.Namespace, start: datetime.datetime) -> dict:
    return {
        "trial_id": args.trial_id,
        "date": start.date().isoformat(),
        "start_time": start.isoformat(timespec="seconds"),
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
        "impacts_made": args.impacts,
        "sensor_mount": {"method": args.mount_method, "preload": args.preload},
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
    p.add_argument("--distance-m", type=float, help="impact point to sensor center")
    p.add_argument("--excitation-type", help="e.g. drop, stomp, tap")
    p.add_argument("--mass-g", type=float)
    p.add_argument("--drop-height-cm", type=float)
    p.add_argument("--tip")
    p.add_argument("--impacts", type=int,
                   help="number of footsteps, stomps or drops made during the trial")
    p.add_argument("--mount-method", help="e.g. tape plus weight")
    p.add_argument("--preload", help="e.g. mass on top of the sensor")
    p.add_argument("--sensor-serial")
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
