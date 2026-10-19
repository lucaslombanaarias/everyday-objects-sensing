"""det-v1 must give the events it gave on 2026-10-14. The fixture holds the counts of
fs_2m_01 (9 firm steps at 2 m on tile) and the baseline measured from fs_noise_03, the
noise-only trial the analysis used for it; no metadata."""

from pathlib import Path

import numpy as np
import pytest
from test_impacts import FS, signal_with

from everyday_sensing.impacts import DETECT, DETECTION, find_impacts, noise_baseline

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


@pytest.mark.parametrize("gap_s, expected", [(0.15, 1), (0.20, 1), (0.24, 1),
                                             (0.26, 2), (0.30, 2), (0.50, 2)])
def test_impacts_closer_than_the_min_gap_are_merged(gap_s, expected):
    # The 0.25 s merge is detector logic, so it is tested here on synthetic impacts rather
    # than with physical close-pair drops, which one person cannot release 0.3 s apart.
    baseline = noise_baseline(signal_with(seed=1), FS)
    result = find_impacts(signal_with([1.0, 1.0 + gap_s]), FS, baseline)
    assert DETECTION.min_gap_s == 0.25
    assert len(result["impacts"]) == expected


@pytest.mark.parametrize("strong_s, weak_s", [(1.0, 1.1), (1.1, 1.0)])
def test_merged_pair_keeps_the_stronger_impact(strong_s, weak_s):
    # The weak impact is 0.1 s from the strong one, so all of it, ringing included, lies
    # inside the 0.25 s gap. There is no dead time: a weak burst's ringing more than 0.25 s
    # after a stronger peak is an event of its own.
    baseline = noise_baseline(signal_with(seed=1), FS)
    pair = (signal_with([strong_s]) + signal_with([weak_s], amplitude=60.0, seed=3)
            - signal_with(seed=3))
    (kept,) = find_impacts(pair, FS, baseline)["impacts"]
    assert round(kept["t_s"], 1) == strong_s
