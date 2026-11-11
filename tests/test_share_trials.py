import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "share_trials.py"
spec = importlib.util.spec_from_file_location("share_trials", SCRIPT)
share_trials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(share_trials)


@pytest.mark.parametrize("new, changed, message", [
    ({"a": "toilet", "b": "toilet"}, False, "Add 2 toilet trials from 2026-11-04"),
    ({"a": "carpet", "b": "wood", "c": None}, False,
     "Add 3 carpet and wood trials from 2026-11-04"),
    ({"a": None}, True, "Add 1 trial from 2026-11-04; update the others"),
    ({}, True, "Update the 2026-11-04 trials and their analysis"),
    ({}, False, "Update the analysis of the 2026-11-04 trials"),
])
def test_commit_message_says_what_is_new(new, changed, message):
    assert share_trials.commit_message("2026-11-04", new, changed) == message


def git(repo, *args):
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
                   cwd=repo, check=True, capture_output=True)


def write_trial(folder: Path, trial: str, floor: str, notes: str = "") -> None:
    (folder / f"{trial}.json").write_text(
        json.dumps({"trial_id": trial, "floor_material": floor, "notes": notes}), encoding="utf-8")
    (folder / f"{trial}.mat").write_bytes(b"mat")


def test_staged_changes_tells_new_trials_from_updated_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(share_trials, "REPO", tmp_path)
    dst = tmp_path / "data" / "controlled" / "raw" / "2026-11-04"
    analysis = tmp_path / "data" / "controlled" / "analysis" / "2026-11-04"
    dst.mkdir(parents=True)
    analysis.mkdir(parents=True)
    git(tmp_path, "init", "-q")
    write_trial(dst, "toilet_base_ice_01", "toilet")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "first share")

    (analysis / "impacts_counts.csv").write_text("tags\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    assert share_trials.staged_changes(dst) == ({}, False)

    write_trial(dst, "toilet_base_pour_01", "toilet")
    git(tmp_path, "add", ".")
    assert share_trials.staged_changes(dst) == ({"toilet_base_pour_01": "toilet"}, False)

    write_trial(dst, "toilet_base_ice_01", "toilet", notes="edited")
    git(tmp_path, "add", ".")
    assert share_trials.staged_changes(dst) == ({"toilet_base_pour_01": "toilet"}, True)
