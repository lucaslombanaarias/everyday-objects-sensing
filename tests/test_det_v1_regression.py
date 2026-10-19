"""det-v1 must give the events it gave on 2026-10-14. The fixture holds the counts of
fs_2m_01 (9 firm steps at 2 m on tile) and the baseline measured from fs_noise_03, the
noise-only trial the analysis used for it; no metadata."""

from pathlib import Path

import numpy as np

from everyday_sensing.impacts import DETECT, DETECTION, find_impacts

FIXTURE = Path(__file__).parent / "fixtures" / "fs_2m_01.npz"

# From impacts_trials.csv of 2026-10-14 (commit 001bd70), before det-v1 was frozen.
TIMES_S = [3.378, 3.987, 5.147, 6.333, 6.903, 7.47, 7.985]
LEVELS_DB = [23.06, 15.11, 14.48, 15.12, 25.41, 14.27, 16.02]


def test_fs_2m_01_gives_the_same_7_events():
    data = np.load(FIXTURE)
    baseline = dict(zip(data["baseline_names"].tolist(), data["baseline_counts"].tolist(),
                        strict=True))
    result = find_impacts(data["counts"], float(data["fs_hz"]), baseline,
                          DETECTION.threshold_db, DETECTION.min_gap_s)
    assert DETECTION.version == "det-v1"
    assert len(result["impacts"]) == 7
    assert [round(i["t_s"], 3) for i in result["impacts"]] == TIMES_S
    assert [round(i["levels_db"][DETECT], 2) for i in result["impacts"]] == LEVELS_DB
