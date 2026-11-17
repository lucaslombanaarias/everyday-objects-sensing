"""Time from a drop-stand impact to its first bounce, on the 2026-10-21 trials.

    python scripts/drop_bounce.py

The drop stand's mass bounces after it lands, and the sensor records the bounce as a second
impact. det-v1 merges events closer than 0.25 s, so the bounce is hidden inside the drop's
event; this looks inside that window. For every 4 in (10.2 cm) drop it takes the detection
level (5 ms RMS above 100 Hz, dB above the block's noise baseline), finds its peaks at least
14 dB above the baseline, 10 ms apart and 6 dB prominent, and takes as the first bounce the
first peak 0.04 to 0.25 s after the drop that is within 30 dB of it.

A mass that falls freely from height h and rebounds with restitution e is in the air for
e * 2 * sqrt(2 h / g) before it lands again, 0.288 s at most from 4 in, so the time to the
first bounce gives e. The stand guides the mass, and friction in it makes the fall slower
than free fall, so e is a lower bound. Prints, per floor and condition, the bounce times,
e and the share of the impact energy lost (1 - e^2).

Why it matters: the bounce is set by the floor and the stand, not by the sensor, which sits
1 to 3 m away. A bounce time that differs between the bare and the enclosed block on the same
floor means the excitation changed between the blocks, so a bare-against-enclosed difference
on that floor is not the enclosure alone. It also explains the 2 or 3 det-v1 events per drop
on wood: later bounces land past the 0.25 s merge window.
"""

from pathlib import Path

import numpy as np
from scipy import signal
from scipy.stats import mannwhitneyu

from everyday_sensing import impacts

REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "data" / "controlled" / "raw" / "2026-10-21"
BASELINES = {("carpet", "bare"): "carpet_bare_noise_01", ("wood", "bare"): "wood_bare_noise_01",
             ("wood", "enclosed"): "wood_bare_noise_01",
             ("carpet", "enclosed"): "wood_enclosed_noise_01"}
"""The noise baseline det-v1 used for each block (see data/controlled/README.md)."""
DROP_HEIGHT_M = 4 * 0.0254
MAX_FLIGHT_S = 2 * np.sqrt(2 * DROP_HEIGHT_M / 9.81)
"""Time in the air after a perfectly elastic bounce (e = 1)."""


def first_bounces(raw: Path = RAW) -> dict:
    """{(floor, condition): [(trial, time to the first bounce in s, or None)]}."""
    baselines = {name: impacts.noise_baseline(*impacts.load_trial(raw / f"{name}.json")[1:])
                 for name in set(BASELINES.values())}
    out = {}
    for path in sorted(raw.glob("*drop4in*.json")):
        floor, condition = path.stem.split("_")[:2]
        _, x, fs = impacts.load_trial(path)
        traces = impacts.detection_traces(x, fs, baselines[BASELINES[(floor, condition)]])
        t, level = traces["rms_t_s"], traces["level_db"]
        drop = int(np.argmax(level))
        peaks, _ = signal.find_peaks(level, height=impacts.DETECTION.threshold_db, distance=10,
                                     prominence=6)
        later = [float(t[p] - t[drop]) for p in peaks
                 if 0.04 < t[p] - t[drop] < 0.25 and level[p] >= level[drop] - 30]
        out.setdefault((floor, condition), []).append((path.stem, later[0] if later else None))
    return out


def main() -> None:
    bounces = first_bounces()
    print(f"longest possible bounce from 4 in: {1000 * MAX_FLIGHT_S:.0f} ms")
    times = {}
    for key, trials in bounces.items():
        found = np.array([t for _, t in trials if t is not None])
        times[key] = found
        e = found / MAX_FLIGHT_S
        print(f"{key[0]}, {key[1]}: bounce found in {found.size} of {len(trials)} drops, "
              f"{1000 * found.min():.0f} to {1000 * found.max():.0f} ms "
              f"(median {1000 * np.median(found):.0f}), e {np.median(e):.2f}, "
              f"energy lost {100 * (1 - np.median(e) ** 2):.0f}%")
    for a, b in ((("carpet", "bare"), ("wood", "bare")), (("wood", "bare"), ("wood", "enclosed")),
                 (("carpet", "bare"), ("carpet", "enclosed"))):
        p = mannwhitneyu(times[a], times[b]).pvalue
        print(f"{' '.join(a)} against {' '.join(b)}: Mann-Whitney p = {p:.3f}")


if __name__ == "__main__":
    main()
