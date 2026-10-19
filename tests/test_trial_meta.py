import datetime
import json

import numpy as np
import pytest
from test_impacts import save, signal_with

from acquisition import record
from acquisition.record import (
    build_meta,
    first_sample_offset_s,
    load_meta,
    normalize_meta,
    parse_args,
    validate_meta,
)
from everyday_sensing.impacts import analyze_folder

START = datetime.datetime(2026, 10, 18, 21, 0, 0, 250000, tzinfo=datetime.UTC)
BASE = ["--port", "COM3", "--trial-id", "t", "--duration", "5"]
GOOD = ["--excitation-type", "ball_drop", "--impacts", "1", "--mount-method", "putty_wax",
        "--distance-m", "0.5", "--excitation-point", "P1", "--floor-material", "tile",
        "--drop-height-cm", "30", "--tip", "steel ball", "--baseline-noise-id", "fs_noise_01"]


def meta_for(argv):
    return build_meta(parse_args(BASE + argv), START)


def test_new_fields_are_recorded():
    meta = meta_for(GOOD)
    assert validate_meta(meta) == []
    assert meta["meta_version"] == 2
    assert meta["excitation"] == {"type": "ball_drop", "mass_g": None, "drop_height_cm": 30.0,
                                  "tip": "steel ball"}
    assert meta["excitation_point_id"] == "P1"
    assert meta["sensor_mount"]["method"] == "putty_wax"
    assert meta["distance_m"] == 0.5
    assert meta["baseline_noise_id"] == "fs_noise_01"
    assert meta["start_time"] == "2026-10-18T21:00:00.250+00:00"


@pytest.mark.parametrize("drop, value, problem", [
    ("--impacts", None, "impacts made is required"),
    ("--impacts", "0", "impacts made is required"),
    ("--mount-method", None, "mount must be one of"),
    ("--mount-method", "tape plus weight", "mount must be one of"),
    ("--excitation-type", "footsteps", "excitation must be one of"),
])
def test_invalid_metadata_is_reported(drop, value, problem):
    argv = list(GOOD)
    i = argv.index(drop)
    if value is None:
        del argv[i:i + 2]
    else:
        argv[i + 1] = value
    problems = validate_meta(meta_for(argv))
    assert len(problems) == 1 and problems[0].startswith(problem)


def test_noise_only_needs_a_mount_and_records_zero_impacts():
    meta = meta_for(["--noise-only", "--mount-method", "tape"])
    assert validate_meta(meta) == []
    assert meta["impacts_made"] == 0
    assert validate_meta(meta_for(["--noise-only"]))[0].startswith("mount must be one of")


def test_recorder_refuses_before_opening_the_port(tmp_path, capsys):
    argv = BASE + ["--out-dir", str(tmp_path), "--excitation-type", "walking",
                   "--mount-method", "tape"]
    assert record.main(argv) == 2
    assert "impacts made is required" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_legacy_sidecar_loads_with_new_fields_null(tmp_path):
    save(tmp_path, "fs_noise_01", signal_with(seed=1), 0, ["--noise-only"])
    save(tmp_path, "fs_2m_01", signal_with([1.0, 2.0]), 1,
         ["--distance-m", "2", "--excitation-type", "footsteps", "--impacts", "2"])
    folder = tmp_path / "2026-10-14"
    # Rewrite both as acquisition.record wrote them on 2026-10-14.
    for name in ("fs_noise_01", "fs_2m_01"):
        path = folder / f"{name}.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        for key in ("meta_version", "excitation_point_id", "baseline_noise_id"):
            del meta[key]
        meta["start_time"] = meta["start_time"].replace(".000", "")
        path.write_text(json.dumps(meta), encoding="utf-8")

    meta = load_meta(folder / "fs_2m_01.json")
    assert meta["meta_version"] is None
    assert meta["excitation_point_id"] is None and meta["baseline_noise_id"] is None
    assert meta["excitation"]["type"] == "footsteps"
    trials = analyze_folder(folder)
    assert [len(t["result"]["impacts"]) for t in trials if t["result"]] == [2]
    assert normalize_meta({"trial_id": "x"})["sensor_mount"] == {"method": None, "preload": None}


def test_first_sample_time_from_first_read():
    # Two frames of 128 samples arrived in the first read at 0.1 s, the next at 0.11 s.
    host = np.repeat([0.1, 0.1, 0.11], 128)
    assert first_sample_offset_s(host, 48000.0) == pytest.approx(0.1 - 256 / 48000)
    assert first_sample_offset_s(np.zeros(0), 48000.0) == 0.0
