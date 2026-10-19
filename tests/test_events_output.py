import csv
import json

import pytest
from test_impacts import save, signal_with

from everyday_sensing.impacts import (
    EVENT_COLUMNS,
    analysis_folder,
    analyze_folder,
    read_events,
    save_tags,
    write_outputs,
)


@pytest.fixture
def day(tmp_path):
    """A noise-only trial and a 4 s trial with steps at 0.5, 2.0 and 3.5 s, 2 made."""
    raw = tmp_path / "raw"
    save(raw, "fs_noise_01", signal_with(seed=1), 0, ["--noise-only", "--mount-method", "tape"])
    save(raw, "fs_1m_01", signal_with([0.5, 2.0, 3.5]), 1,
         ["--distance-m", "1", "--excitation-type", "walking", "--impacts", "2",
          "--mount-method", "tape", "--baseline-noise-id", "fs_noise_02"])
    return raw / "2026-10-14"


def test_events_csv_columns_times_and_edge_flags(day):
    out = write_outputs(analyze_folder(day), analysis_folder(day))
    rows = read_events(out / "events_fs_1m_01.csv")
    with (out / "events_fs_1m_01.csv").open(encoding="utf-8") as f:
        assert next(csv.reader(f)) == list(EVENT_COLUMNS)
    assert [r["event"] for r in rows] == ["1", "2", "3"]
    assert [round(float(r["t_s"]), 1) for r in rows] == [0.5, 2.0, 3.5]
    # Events in the first or last 1.0 s are flagged, not dropped.
    assert [r["flag"] for r in rows] == ["edge", "", "edge"]
    assert all(r["annotation"] == "unreviewed" for r in rows)
    assert all(float(r["peak_db"]) >= 14 for r in rows)
    # start_time is 2026-10-14T22:01:00.000+00:00 (save() starts trials a minute apart).
    assert rows[1]["timestamp"].startswith("2026-10-14T22:01:02.0")
    assert rows[1]["timestamp"].endswith("+00:00")


def test_counts_reconcile_made_and_found(day):
    out = write_outputs(analyze_folder(day), analysis_folder(day))
    with (out / "impacts_counts.csv").open(encoding="utf-8") as f:
        (row,) = list(csv.DictReader(f))
    assert (row["impacts_made"], row["impacts_found"], row["difference"]) == ("2", "3", "1")
    assert (row["edge_events"], row["unreviewed"], row["mount"]) == ("2", "3", "tape")
    assert row["detection_version"] == "det-v1"
    # The sidecar names fs_noise_02 as the block's baseline; the analysis used fs_noise_01.
    assert (row["baseline_used"], row["baseline_mismatch"]) == ("fs_noise_01", "True")
    record = json.loads((out / "fs_1m_01.analysis.json").read_text(encoding="utf-8"))
    assert record["difference"] == 1 and len(record["events"]) == 3


def test_tags_are_saved_and_survive_reanalysis(day):
    out = write_outputs(analyze_folder(day), analysis_folder(day))
    path = out / "events_fs_1m_01.csv"
    save_tags(path, {1: "walk_in_out", 2: "true_impact"})
    write_outputs(analyze_folder(day), out)
    assert [r["annotation"] for r in read_events(path)] == [
        "walk_in_out", "true_impact", "unreviewed"]
    with (out / "impacts_counts.csv").open(encoding="utf-8") as f:
        (row,) = list(csv.DictReader(f))
    assert (row["true_impact"], row["walk_in_out"]) == ("1", "1")
    with pytest.raises(ValueError):
        save_tags(path, {3: "footstep"})


def test_detection_traces_match_the_events():
    from test_impacts import FS

    from everyday_sensing.impacts import DETECT, detection_traces, find_impacts, noise_baseline

    x = signal_with([1.0, 2.0])
    baseline = noise_baseline(signal_with(seed=1), FS)
    traces = detection_traces(x, FS, baseline)
    assert traces["highpassed"].size == x.size
    assert traces["threshold_counts"] == pytest.approx(baseline[DETECT] * 10 ** (14 / 20))
    for imp in find_impacts(x, FS, baseline)["impacts"]:
        k = int(round(imp["t_s"] * 1000)) - 1
        assert traces["rms_t_s"][k] == pytest.approx(imp["t_s"])
        assert traces["level_db"][k] == pytest.approx(imp["levels_db"][DETECT])
