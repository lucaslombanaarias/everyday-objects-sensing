import datetime
from pathlib import Path

import numpy as np
import pytest

from acquisition.record import (
    Capture,
    build_meta,
    distance_label,
    next_trial_id,
    parse_args,
    write_trial,
)
from everyday_sensing.impacts import (
    BANDS,
    analyze_folder,
    find_impacts,
    noise_baseline,
    summarize,
    trial_rows,
)

FS = 48000.0
START = datetime.datetime(2026, 10, 14, 22, 0, tzinfo=datetime.UTC)


def signal_with(steps=(), clicks=(), duration_s=4.0, amplitude=300.0, seed=0):
    """Noise plus 'steps' (decaying 300 Hz and 1 kHz bursts, like a footstep below 2 kHz)
    and 'clicks' (6 kHz only, like a shoe on tile)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration_s * FS)) / FS
    x = 2.0 * rng.standard_normal(t.size)

    def burst(t0, f, a, tau):
        dt = t - t0
        return np.where(dt >= 0, a * np.exp(-dt / tau) * np.sin(2 * np.pi * f * dt), 0.0)

    for t0 in steps:
        x += burst(t0, 300, amplitude, 0.04) + burst(t0, 1000, amplitude, 0.02)
    for t0 in clicks:
        # Smooth (Gaussian) envelope so the energy stays near 6 kHz, as the real clicks did.
        x += amplitude * np.exp(-(((t - t0) / 0.003) ** 2)) * np.sin(2 * np.pi * 6000 * t)
    return x


def save(folder, trial_id, x, minutes, argv):
    capture = Capture(
        sample_index=np.arange(x.size),
        counts=np.clip(np.round(x), -32768, 32767).astype(np.int16),
        host_time_s=np.arange(x.size) / FS,
        checks={"ok": True},
    )
    args = parse_args(["--port", "SIM", "--trial-id", trial_id, "--duration", "4", *argv])
    meta = build_meta(args, START + datetime.timedelta(minutes=minutes))
    write_trial(capture, meta, folder)


def test_steps_found_and_clicks_ignored():
    steps, clicks = [1.0, 1.8, 2.6], [0.4, 3.4]
    x = signal_with(steps, clicks)
    baseline = noise_baseline(signal_with(duration_s=3.0, seed=1), FS)
    result = find_impacts(x, FS, baseline)
    times = [i["t_s"] for i in result["impacts"]]
    assert len(times) == len(steps)
    np.testing.assert_allclose(times, steps, atol=0.06)
    for i in result["impacts"]:
        assert set(i["levels_db"]) == set(BANDS)
        assert i["levels_db"]["100-500 Hz"] > 20


def test_quietest_stretch_used_without_baseline():
    result = find_impacts(signal_with([1.0, 2.5]), FS)
    assert result["baseline_source"] == "quietest 2 s of the trial"
    assert len(result["impacts"]) == 2


def test_min_gap_merges_close_impacts():
    result = find_impacts(signal_with([1.0, 1.15]), FS, min_gap_s=0.3)
    assert len(result["impacts"]) == 1


def test_folder_uses_latest_preceding_noise_trial(tmp_path):
    save(tmp_path, "fs_noise_01", signal_with(seed=1), 0, ["--noise-only"])
    save(tmp_path, "fs_1m_01", signal_with([1.0, 2.0]), 1,
         ["--distance-m", "1", "--excitation-type", "footsteps", "--impacts", "2"])
    save(tmp_path, "fs_noise_02", signal_with(seed=2), 2, ["--noise-only"])
    save(tmp_path, "fs_2m_01", signal_with([1.0, 2.0, 3.0], amplitude=100), 3,
         ["--distance-m", "2", "--excitation-type", "footsteps", "--impacts", "3"])
    folder = tmp_path / "2026-10-14"

    trials = analyze_folder(folder)
    by_id = {t["meta"]["trial_id"]: t for t in trials}
    assert by_id["fs_noise_01"]["result"] is None
    assert by_id["fs_1m_01"]["result"]["baseline_source"] == "fs_noise_01"
    assert by_id["fs_2m_01"]["result"]["baseline_source"] == "fs_noise_02"

    rows = summarize(trials)
    counts = [(r["distance_m"], r["trials"], r["impacts_found"], r["impacts_made"]) for r in rows]
    assert counts == [(1.0, 1, 2, 2), (2.0, 1, 3, 3)]
    # A third of the amplitude is about 9.5 dB lower.
    drop = rows[0]["100-500 Hz mean dB"] - rows[1]["100-500 Hz mean dB"]
    assert drop == pytest.approx(20 * np.log10(3), abs=1.5)
    assert len(trial_rows(trials)) == 5


def test_chosen_baseline_applies_to_every_trial(tmp_path):
    save(tmp_path, "fs_noise_01", signal_with(seed=1), 0, ["--noise-only"])
    save(tmp_path, "fs_1m_01", signal_with([1.0]), 1,
         ["--distance-m", "1", "--excitation-type", "footsteps"])
    # Twice the noise: every level measured against it is about 6 dB lower.
    save(tmp_path, "fs_noise_02", 2 * signal_with(seed=2), 2, ["--noise-only"])
    folder = tmp_path / "2026-10-14"
    auto = analyze_folder(folder)[1]["result"]
    forced = analyze_folder(folder, baseline_trial="fs_noise_02")[1]["result"]
    assert auto["baseline_source"] == "fs_noise_01"
    assert forced["baseline_source"] == "fs_noise_02"
    a = auto["impacts"][0]["levels_db"]["100-500 Hz"]
    b = forced["impacts"][0]["levels_db"]["100-500 Hz"]
    assert a - b == pytest.approx(20 * np.log10(2), abs=1.0)
    with pytest.raises(ValueError):
        analyze_folder(folder, baseline_trial="fs_noise_99")


def test_trial_still_being_written_is_skipped(tmp_path):
    save(tmp_path, "fs_noise_01", signal_with(seed=1), 0, ["--noise-only"])
    save(tmp_path, "fs_1m_01", signal_with([1.0]), 1,
         ["--distance-m", "1", "--excitation-type", "footsteps"])
    folder = tmp_path / "2026-10-14"
    (folder / "fs_1m_01.mat").write_bytes(b"MATLAB 5.0 MAT-file, truncated")
    trials = analyze_folder(folder)
    assert [t["meta"]["trial_id"] for t in trials] == ["fs_noise_01"]


def test_sidecar_is_written_last(tmp_path, monkeypatch):
    import acquisition.record as record

    seen = []
    real = record.scipy.io.savemat

    def savemat(path, *args, **kwargs):
        seen.append(Path(path).with_suffix(".json").exists())
        return real(path, *args, **kwargs)

    monkeypatch.setattr(record.scipy.io, "savemat", savemat)
    save(tmp_path, "fs_1m_01", signal_with([1.0]), 1,
         ["--distance-m", "1", "--excitation-type", "footsteps"])
    assert seen == [False]


def test_trash_trial_outside_windows_moves_to_trash_folder(tmp_path, monkeypatch):
    import acquisition.record as record

    monkeypatch.setattr(record.sys, "platform", "linux")
    save(tmp_path, "fs_noise_01", signal_with(seed=1), 0, ["--noise-only"])
    save(tmp_path, "fs_1m_01", signal_with([1.0]), 1,
         ["--distance-m", "1", "--excitation-type", "footsteps"])
    folder = tmp_path / "2026-10-14"
    where = record.trash_trial(folder / "fs_1m_01.json")
    assert sorted(p.name for p in Path(where).iterdir()) == [
        "fs_1m_01.csv", "fs_1m_01.json", "fs_1m_01.mat"]
    assert record.trial_files(folder / "fs_1m_01.json") == []
    assert [t["meta"]["trial_id"] for t in analyze_folder(folder)] == ["fs_noise_01"]


def test_cache_reuses_results(tmp_path):
    save(tmp_path, "fs_noise_01", signal_with(seed=1), 0, ["--noise-only"])
    save(tmp_path, "fs_1m_01", signal_with([1.0]), 1,
         ["--distance-m", "1", "--excitation-type", "footsteps"])
    cache = {}
    first = analyze_folder(tmp_path / "2026-10-14", cache=cache)
    n = len(cache)
    second = analyze_folder(tmp_path / "2026-10-14", cache=cache)
    assert len(cache) == n
    assert second[1]["result"] is first[1]["result"]


def test_trial_ids(tmp_path):
    assert distance_label(1) == "1m"
    assert distance_label(0.5) == "0p5m"
    assert next_trial_id(tmp_path, "fs", "footsteps", 1) == "fs_1m_01"
    for name in ("fs_1m_01.json", "fs_1m_03.json", "fs_1m_extra.json", "fs_noise_01.json"):
        (tmp_path / name).write_text("{}")
    assert next_trial_id(tmp_path, "fs", "footsteps", 1) == "fs_1m_04"
    assert next_trial_id(tmp_path, "fs", "noise only", 1) == "fs_noise_02"
    assert next_trial_id(tmp_path, "fs", "footsteps", 2) == "fs_2m_01"


def test_impacts_flag_recorded_and_rejected_for_noise():
    args = parse_args(["--port", "COM3", "--trial-id", "t", "--duration", "1", "--impacts", "5"])
    assert build_meta(args, START)["impacts_made"] == 5
    with pytest.raises(SystemExit):
        parse_args(["--port", "COM3", "--trial-id", "t", "--duration", "1", "--noise-only",
                    "--impacts", "5"])
