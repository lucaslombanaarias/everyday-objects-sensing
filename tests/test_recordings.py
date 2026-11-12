import datetime
import importlib.util
import json
import shutil
import types
from pathlib import Path

import pytest
import scipy.io
from test_impacts import START, save, signal_with

from acquisition.record import (
    edit_trial,
    find_board_port,
    list_recordings,
    next_trial_id,
    rename_trial,
)
from everyday_sensing import impacts

DAY = START.date().isoformat()
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "share_trials.py"
spec = importlib.util.spec_from_file_location("share_trials", SCRIPT)
share_trials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(share_trials)


def port(device, vid=None):
    return types.SimpleNamespace(device=device, vid=vid)


def test_board_port_is_the_espressif_one():
    bluetooth = [port("COM3"), port("COM4")]
    assert find_board_port([*bluetooth, port("COM7", 0x303A)]) == "COM7"
    # Bluetooth ports and other USB devices are never taken for the board.
    assert find_board_port(bluetooth) is None
    assert find_board_port([*bluetooth, port("COM9", 0x2341)]) is None
    assert find_board_port([port("COM12", 0x303A), port("COM11", 0x303A)]) == "COM11"


def test_trial_id_without_prefix_or_radius(tmp_path):
    assert next_trial_id(tmp_path, "trial", None, None) == "trial_01"
    (tmp_path / "trial_01.json").write_text("{}")
    (tmp_path / "trial_1m_01.json").write_text("{}")
    assert next_trial_id(tmp_path, "trial", "stomp", None) == "trial_02"
    assert next_trial_id(tmp_path, "trial", "noise only", None) == "trial_noise_01"
    assert next_trial_id(tmp_path, "trial", None, 1.0) == "trial_1m_02"
    # Numbered after the day's shared trials too (another computer's trial_03, say).
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "trial_03.json").write_text("{}")
    assert next_trial_id(tmp_path, "trial", None, None, [shared]) == "trial_04"
    assert next_trial_id(tmp_path, "trial", None, None, [tmp_path / "missing"]) == "trial_02"


@pytest.fixture
def day(tmp_path):
    folder = tmp_path / "raw" / DAY
    save(folder.parent, "a_noise_01", signal_with(duration_s=1.0), 0, ["--noise-only"])
    save(folder.parent, "a_01", signal_with([0.5], duration_s=1.0), 1,
         ["--notes", "first", "--baseline-noise-id", "a_noise_01"])
    return folder


def test_edit_changes_the_sidecar_and_the_mat_and_keeps_the_old_values(day):
    path = day / "a_01.json"
    changed = edit_trial(path, {"notes": "stomps", "excitation.type": "stomp",
                                "impacts_made": 3, "sensor_mount.method": "tape"},
                         today=datetime.date(2026, 11, 11))
    assert changed == ["notes", "excitation.type", "impacts_made", "sensor_mount.method"]
    meta = json.loads(path.read_text(encoding="utf-8"))
    assert meta["notes"] == "stomps"
    assert meta["excitation"]["type"] == "stomp"
    assert meta["sensor_mount"] == {"method": "tape", "preload": None}
    assert meta["edit_note"] == (
        "2026-11-11: edited in the dashboard; before: notes 'first', excitation.type None, "
        "impacts_made None, sensor_mount.method None")
    mat = scipy.io.loadmat(day / "a_01.mat")
    assert mat["meta"]["notes"][0, 0][0] == "stomps"
    assert mat["meta"]["edit_note"][0, 0][0] == meta["edit_note"]
    assert mat["counts"].shape == (48000, 1)

    # A second edit adds to the note; an edit that changes nothing writes nothing.
    edit_trial(path, {"notes": "stomps at 1 m"}, today=datetime.date(2026, 11, 12))
    meta = json.loads(path.read_text(encoding="utf-8"))
    assert meta["edit_note"].endswith(
        "; 2026-11-12: edited in the dashboard; before: notes 'stomps'")
    before = path.stat().st_mtime_ns
    assert edit_trial(path, {"notes": "stomps at 1 m"}) == []
    assert path.stat().st_mtime_ns == before
    with pytest.raises(ValueError, match="cannot edit trial_id"):
        edit_trial(path, {"trial_id": "b"})


def test_rename_moves_the_files_and_follows_the_trial(day):
    out = impacts.analysis_folder(day)
    impacts.write_outputs(impacts.analyze_folder(day), out)
    impacts.save_tags(out / "events_a_01.csv", {1: "true_impact"})
    variant = out / "baseline_a_noise_01"
    impacts.write_outputs(impacts.analyze_folder(day, baseline_trial="a_noise_01"), variant)

    new = rename_trial(day / "a_noise_01.json", "tile_noise_01",
                       today=datetime.date(2026, 11, 11))
    impacts.rename_outputs(out, "a_noise_01", "tile_noise_01")

    assert new == day / "tile_noise_01.json"
    assert sorted(p.name for p in day.iterdir()) == [
        "a_01.csv", "a_01.json", "a_01.mat",
        "tile_noise_01.csv", "tile_noise_01.json", "tile_noise_01.mat"]
    rename_trial(day / "a_01.json", "tile_1m_01")
    impacts.rename_outputs(out, "a_01", "tile_1m_01")
    meta = json.loads(new.read_text(encoding="utf-8"))
    assert meta["trial_id"] == "tile_noise_01"
    assert meta["files"] == {"csv": "tile_noise_01.csv", "mat": "tile_noise_01.mat"}
    assert meta["edit_note"] == ("2026-11-11: renamed from a_noise_01 to tile_noise_01 in the "
                                 "dashboard")
    mat = scipy.io.loadmat(day / "tile_noise_01.mat")
    assert mat["meta"]["trial_id"][0, 0][0] == "tile_noise_01"
    assert mat["counts"].shape == (48000, 1)
    # The trial measured against it names the new noise trial now.
    other = json.loads((day / "tile_1m_01.json").read_text(encoding="utf-8"))
    assert other["baseline_noise_id"] == "tile_noise_01"
    assert "before: baseline_noise_id 'a_noise_01'" in other["edit_note"]
    # The analysis follows the trials, tags included, and runs with the new names.
    assert impacts.read_events(out / "events_tile_1m_01.csv")[0]["annotation"] == "true_impact"
    assert not (out / "events_a_noise_01.csv").exists()
    record = json.loads((out / "tile_noise_01.analysis.json").read_text(encoding="utf-8"))
    assert record["trial_id"] == "tile_noise_01"
    assert (out / "baseline_tile_noise_01" / "events_tile_noise_01.csv").exists()
    assert not variant.exists()
    trials = impacts.analyze_folder(day)
    assert [t["meta"]["trial_id"] for t in trials] == ["tile_noise_01", "tile_1m_01"]


def test_rename_refuses_taken_and_bad_names(day):
    with pytest.raises(FileExistsError, match="a_01.json"):
        rename_trial(day / "a_noise_01.json", "a_01")
    with pytest.raises(ValueError, match="cannot be a trial ID"):
        rename_trial(day / "a_noise_01.json", "a/b")
    assert rename_trial(day / "a_01.json", "a_01") == day / "a_01.json"
    assert sorted(p.stem for p in day.glob("*.json")) == ["a_01", "a_noise_01"]


def test_share_refuses_to_overwrite_another_recording(day, tmp_path):
    dst = tmp_path / "controlled" / "raw" / DAY
    dst.mkdir(parents=True)
    sidecars = sorted(day.glob("*.json"))
    assert share_trials.taken_by_other_recordings(sidecars, dst) == []
    shutil.copy2(day / "a_01.json", dst / "a_01.json")  # the same recording, shared before
    meta = json.loads((day / "a_noise_01.json").read_text(encoding="utf-8"))
    (dst / "a_noise_01.json").write_text(json.dumps({**meta, "start_time": "another"}))
    assert share_trials.taken_by_other_recordings(sidecars, dst) == ["a_noise_01"]


def test_recordings_say_which_are_shared(day, tmp_path):
    controlled = tmp_path / "controlled"
    assert [r["shared"] for r in list_recordings(day.parent, controlled)] == ["", ""]
    shared = controlled / "raw" / DAY
    shared.mkdir(parents=True)
    shutil.copy2(day / "a_01.json", shared / "a_01.json")
    shutil.copy2(day / "a_noise_01.json", shared / "b_noise_01.json")
    meta = json.loads((shared / "b_noise_01.json").read_text(encoding="utf-8"))
    (shared / "b_noise_01.json").write_text(json.dumps({**meta, "trial_id": "b_noise_01"}))
    rows = list_recordings(day.parent, controlled)
    assert [(r["meta"]["trial_id"], r["shared"]) for r in rows] == [
        ("a_01", "yes"), ("a_noise_01", "as b_noise_01")]
    edit_trial(day / "a_01.json", {"notes": "edited"})
    assert list_recordings(day.parent, controlled)[0]["shared"] == "changed"
