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


def write_recording(folder: Path, trial: str, start: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{trial}.json").write_text(
        json.dumps({"trial_id": trial, "start_time": start, "floor_material": "wood"}),
        encoding="utf-8")
    (folder / f"{trial}.mat").write_bytes(b"mat")


def test_deleted_from_repo_finds_only_the_deleted_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(share_trials, "REPO", tmp_path)
    dst = tmp_path / "data" / "controlled" / "raw" / "2026-11-11"
    local = tmp_path / "data" / "raw" / "2026-11-11"
    git(tmp_path, "init", "-q")
    write_recording(dst, "dummy_01", "2026-11-11T17:00:00.000-06:00")
    write_recording(dst, "kept_01", "2026-11-11T17:05:00.000-06:00")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "share")
    git(tmp_path, "rm", "-q", str(dst / "dummy_01.json"), str(dst / "dummy_01.mat"))
    git(tmp_path, "commit", "-q", "-m", "remove the dummy")

    write_recording(local, "dummy_01", "2026-11-11T17:00:00.000-06:00")
    write_recording(local, "kept_01", "2026-11-11T17:05:00.000-06:00")
    write_recording(local, "new_01", "2026-11-11T17:10:00.000-06:00")
    sidecars = sorted(local.glob("*.json"))
    assert share_trials.deleted_from_repo(sidecars, dst) == ["dummy_01"]

    # A new recording that reuses the deleted name is not the deleted trial.
    write_recording(local, "dummy_01", "2026-11-11T18:00:00.000-06:00")
    assert share_trials.deleted_from_repo(sidecars, dst) == []


def test_share_does_not_put_back_a_deleted_trial(tmp_path, monkeypatch):
    remote, repo = tmp_path / "remote.git", tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "clone", "-q", str(remote), str(repo))
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "test")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "test@example.com")
    monkeypatch.setattr(share_trials, "REPO", repo)
    monkeypatch.setattr(share_trials, "DEFAULT_OUT_DIR", repo / "data" / "raw")
    monkeypatch.setattr(share_trials, "CONTROLLED", repo / "data" / "controlled")
    (repo / ".gitignore").write_text("data/raw/\ndata/analysis/\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "start")
    git(repo, "push", "-q", "-u", "origin", "HEAD:main")

    local = repo / "data" / "raw" / "2026-11-11"
    write_recording(local, "dummy_01", "2026-11-11T17:00:00.000-06:00")
    analysis = repo / "data" / "analysis" / "2026-11-11"
    analysis.mkdir(parents=True)
    (analysis / "dummy_01.analysis.json").write_text("{}", encoding="utf-8")
    (analysis / "events_dummy_01.csv").write_text("event\n", encoding="utf-8")
    share_trials.main(["2026-11-11"])
    dst = repo / "data" / "controlled" / "raw" / "2026-11-11"
    git(repo, "rm", "-q", "-r", str(dst), str(repo / "data" / "controlled" / "analysis"))
    git(repo, "commit", "-q", "-m", "remove the dummy")
    git(repo, "push", "-q")

    write_recording(local, "trial_01", "2026-11-11T17:05:00.000-06:00")
    (analysis / "trial_01.analysis.json").write_text("{}", encoding="utf-8")
    share_trials.main(["2026-11-11"])
    assert sorted(p.name for p in dst.iterdir()) == ["trial_01.json", "trial_01.mat"]
    shared_analysis = repo / "data" / "controlled" / "analysis" / "2026-11-11"
    assert sorted(p.name for p in shared_analysis.iterdir()) == ["trial_01.analysis.json"]

    share_trials.main(["2026-11-11"])  # sharing again changes nothing
    assert sorted(p.name for p in dst.iterdir()) == ["trial_01.json", "trial_01.mat"]
