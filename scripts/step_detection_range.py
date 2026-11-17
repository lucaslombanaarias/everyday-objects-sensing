"""How far from the sensor a step is still detected, from the 2026-10-21 walking trials.

    python scripts/step_detection_range.py

Uses the hand review of the walking trials (analysis/2026-10-21/walking_steps_review.csv):
a trial's steps are its det-v1 events that the review does not list as "not a step", plus
the steps det-v1 missed, each with its level in dB above the noise baseline. The trial the
review left undecided (wood_bare_walk_3m_01) is left out, which leaves 35 trials.

For each floor and condition it prints the share of steps at or above the det-v1 threshold
at 1, 2 and 3 m (measured), and a fit: step level against log10 of distance, with normal
scatter about the line, gives the share detected at any distance and the distance at which
that share falls to 90% and 50%, with a 95% interval from resampling the trials at each
distance. Only 1, 2 and 3 m were walked, so anything past 3 m is the fit, not a measurement,
and the 90% distance on carpet lies between measured distances. On wood the level barely
falls from 2 to 3 m, so its distances are far outside the data and should not be quoted.
"""

import csv
import re
from pathlib import Path

import numpy as np
from scipy.stats import norm

from everyday_sensing import impacts

REPO = Path(__file__).resolve().parents[1]
ANALYSIS = REPO / "data" / "controlled" / "analysis" / "2026-10-21"
THRESHOLD_DB = impacts.DETECTION.threshold_db
RADII = (1.0, 2.0, 3.0)
GROUPS = (("carpet", "bare"), ("carpet", "enclosed"), ("wood", "bare"), ("wood", "enclosed"))
RESAMPLES = 2000


def step_levels(analysis: Path = ANALYSIS) -> list[dict]:
    """One row per reviewed walking trial: floor, condition, radius_m and the levels (dB above
    baseline) of its steps, found and missed."""
    trials = []
    with (analysis / "walking_steps_review.csv").open(newline="") as f:
        review = list(csv.DictReader(f))
    for row in review:
        if not row["steps_in_signal"]:
            continue
        with (analysis / f"events_{row['trial_id']}.csv").open(newline="") as f:
            events = [(float(e["t_s"]), float(e["peak_db"])) for e in csv.DictReader(f)]
        not_steps = [float(t) for t in re.findall(r"([\d.]+) s", row["not_steps_at"])]
        levels = [db for t, db in events if not any(abs(t - s) < 0.06 for s in not_steps)]
        levels += [float(db) for db in re.findall(r"s ([\d.]+) dB", row["missed_at"])]
        if len(levels) != int(row["steps_in_signal"]):
            raise SystemExit(f"{row['trial_id']}: {len(levels)} step levels, review says "
                             f"{row['steps_in_signal']}")
        trials.append({"trial": row["trial_id"], "floor": row["floor"],
                       "condition": row["condition"], "radius_m": float(row["radius_m"]),
                       "levels": levels})
    return trials


def fit(trials: list[dict]) -> tuple[float, float, float]:
    """(level at 1 m, change per tenfold distance, scatter), all in dB."""
    x = np.concatenate([[np.log10(t["radius_m"])] * len(t["levels"]) for t in trials])
    y = np.concatenate([t["levels"] for t in trials])
    slope, at_1m = np.polyfit(x, y, 1)
    return float(at_1m), float(slope), float(np.std(y - (at_1m + slope * x), ddof=2))


def share_detected(model: tuple[float, float, float], radius_m) -> np.ndarray:
    """Fitted share of steps at or above the threshold, 0 to 1."""
    at_1m, slope, scatter = model
    return norm.cdf((at_1m + slope * np.log10(radius_m) - THRESHOLD_DB) / scatter)


def radius_for(model: tuple[float, float, float], share: float) -> float:
    """Distance (m) at which the fitted share detected equals share."""
    at_1m, slope, scatter = model
    if slope >= 0:
        return float("inf")
    return float(10 ** ((THRESHOLD_DB + norm.ppf(share) * scatter - at_1m) / slope))


def measured(trials: list[dict], radius_m: float) -> dict:
    """Share of steps at or above the threshold at one distance, and the range over trials."""
    here = [np.array(t["levels"]) >= THRESHOLD_DB for t in trials if t["radius_m"] == radius_m]
    return {"share": float(np.concatenate(here).mean()), "steps": int(sum(h.size for h in here)),
            "detected": int(sum(h.sum() for h in here)),
            "low": float(min(h.mean() for h in here)), "high": float(max(h.mean() for h in here))}


def group(trials: list[dict], floor: str, condition: str) -> list[dict]:
    return [t for t in trials if t["floor"] == floor and t["condition"] == condition]


def interval(trials: list[dict], share: float, seed: int = 0) -> tuple[float, float]:
    """95% interval of radius_for, resampling the trials at each distance."""
    rng = np.random.default_rng(seed)
    by_radius = [[t for t in trials if t["radius_m"] == r] for r in RADII]
    values = []
    for _ in range(RESAMPLES):
        sample = [ts[i] for ts in by_radius for i in rng.integers(0, len(ts), len(ts))]
        values.append(radius_for(fit(sample), share))
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high)


def main() -> None:
    trials = step_levels()
    print(f"{len(trials)} reviewed walking trials, threshold {THRESHOLD_DB:g} dB")
    for floor, condition in GROUPS:
        g = group(trials, floor, condition)
        model = fit(g)
        shares = [measured(g, r) for r in RADII]
        print(f"{floor}, {condition}: detected "
              + ", ".join(f"{m['detected']} of {m['steps']} at {r:g} m ({100 * m['share']:.0f}%)"
                          for r, m in zip(RADII, shares, strict=True)))
        print(f"    fit: {model[0]:.1f} dB at 1 m, {model[1] * np.log10(2):+.1f} dB per doubling, "
              f"scatter {model[2]:.1f} dB")
        for share in (0.9, 0.5):
            low, high = interval(g, share)
            print(f"    {100 * share:.0f}% of steps detected out to "
                  f"{radius_for(model, share):.2f} m (95% interval {low:.2f} to {high:.2f} m)")


if __name__ == "__main__":
    main()
