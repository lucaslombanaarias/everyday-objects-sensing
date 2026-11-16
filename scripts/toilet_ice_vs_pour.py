"""Tell an ice drop from the splashes of a water pour, event by event, on the toilet trials.

    python scripts/toilet_ice_vs_pour.py

Trains everyday_sensing.event_classifier (8 features per det-v1 event, logistic regression)
on the 2026-11-04 trials (bare sensor at the base, behind the seat and on the tank lid) and
tests it once on the 2026-11-11 trials (sensor in its enclosure behind the seat, mounted
loose), which it has not seen. Prints:

- 2026-11-04, each trial held out in turn within its position: events labelled correctly.
- 2026-11-11, trained on all of 2026-11-04: events labelled correctly, and the same trained
  on the behind-seat trials only.

Events and labels. Every trial is measured against its session's noise baseline. An ice
event is a drop: an event tagged true_impact in analysis/<date>/events_<trial>.csv, 5 per
ice trial (45 on 2026-11-04, 15 on 2026-11-11); the second hits and splashes after a drop
are tagged other_noise and left out. A pour event is any det-v1 event of a stream trial that
is not flagged edge; det-v1 marks the peaks of the continuous splash, 11 to 33 per pour.
Events with less than 0.35 s of recording before them or 0.18 s after are left out.

What this does and does not show: ice and water were recorded in separate trials, so a drop
during a pour is not tested, and ice and poured water stand in for the real thing.
"""

import csv
from pathlib import Path

import numpy as np

from everyday_sensing import event_classifier as ec
from everyday_sensing import impacts

REPO = Path(__file__).resolve().parents[1]
CONTROLLED = REPO / "data" / "controlled"
TRAIN = ("2026-11-04", "toilet_base_noise_01")
TEST = ("2026-11-11", "Base_noise_01")
"""(session folder, its noise baseline)."""
POSITIONS = ("base", "behindseat", "tanklid")


def _kind(meta: dict, event: dict) -> str | None:
    """"ice", "pour", or None for an event that is neither a drop nor part of a pour."""
    if meta["excitation"]["type"] == "water_pour":
        return None if event["flag"] == "edge" else "pour"
    return "ice" if event["annotation"] == "true_impact" else None


def collect(date: str, noise_id: str) -> list[dict]:
    """One row per labelled event of a session: trial, position, kind, t_s and the features."""
    raw, analysis = CONTROLLED / "raw" / date, CONTROLLED / "analysis" / date
    _, noise, fs = impacts.load_trial(raw / f"{noise_id}.json")
    baseline = impacts.noise_baseline(noise, fs)
    rows = []
    for path in sorted(raw.glob("*.json")):
        meta, x, fs = impacts.load_trial(path)
        if impacts.is_noise(meta):
            continue
        level = impacts.detection_traces(x, fs, baseline)["level_db"]
        hp = ec.highpassed(x, fs)
        position = path.stem.split("_")[1] if date == TRAIN[0] else "behindseat"
        with (analysis / f"events_{path.stem}.csv").open(newline="") as f:
            for event in csv.DictReader(f):
                kind, t = _kind(meta, event), float(event["t_s"])
                if kind is None or not ec.measurable(t, level):
                    continue
                rows.append({"trial": path.stem, "position": position, "kind": kind, "t_s": t,
                             **ec.event_features(hp, fs, level, t)})
    return rows


def _xy(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    x = np.array([[r[name] for name in ec.FEATURES] for r in rows])
    return x, np.array([r["kind"] == "ice" for r in rows])


def held_out_by_trial(rows: list[dict]) -> np.ndarray:
    """P(ice) of every row, from a model trained on the other trials of the row's position."""
    p = np.zeros(len(rows))
    for position in sorted({r["position"] for r in rows}):
        here = [i for i, r in enumerate(rows) if r["position"] == position]
        x, y = _xy([rows[i] for i in here])
        trials = np.array([rows[i]["trial"] for i in here])
        for trial in np.unique(trials):
            out = trials == trial
            p[np.array(here)[out]] = ec.predict_proba(ec.fit_logistic(x[~out], y[~out]), x[out])
    return p


def test_on(train: list[dict], test: list[dict]) -> np.ndarray:
    """P(ice) of every test row, from a model trained on all the train rows."""
    return ec.predict_proba(ec.fit_logistic(*_xy(train)), _xy(test)[0])


def confusion(rows: list[dict], p: np.ndarray) -> dict:
    """Counts by (actual, labelled), and the rows labelled wrongly."""
    said = np.where(p > 0.5, "ice", "pour")
    counts = {(a, b): 0 for a in ("ice", "pour") for b in ("ice", "pour")}
    for row, label in zip(rows, said, strict=True):
        counts[(row["kind"], label)] += 1
    wrong = [(r["trial"], r["t_s"], r["kind"], round(float(q), 2))
             for r, q, label in zip(rows, p, said, strict=True) if label != r["kind"]]
    return {"counts": counts, "correct": counts[("ice", "ice")] + counts[("pour", "pour")],
            "n": len(rows), "wrong": wrong}


def _report(title: str, result: dict) -> None:
    c = result["counts"]
    print(f"{title}: {result['correct']} of {result['n']} correct "
          f"(ice {c[('ice', 'ice')]} of {c[('ice', 'ice')] + c[('ice', 'pour')]}, "
          f"pour {c[('pour', 'pour')]} of {c[('pour', 'pour')] + c[('pour', 'ice')]})")
    for trial, t, kind, p in result["wrong"]:
        print(f"    {trial} at {t:.2f} s is {kind}, P(ice) {p}")


def main() -> None:
    train, test = collect(*TRAIN), collect(*TEST)
    for name, rows in ((TRAIN[0], train), (TEST[0], test)):
        n_ice = sum(r["kind"] == "ice" for r in rows)
        print(f"{name}: {n_ice} ice drops and {len(rows) - n_ice} pour events")
    _report(f"{TRAIN[0]}, each trial held out in turn", confusion(train, held_out_by_trial(train)))
    _report(f"{TEST[0]}, trained on all of {TRAIN[0]}", confusion(test, test_on(train, test)))
    behind = [r for r in train if r["position"] == "behindseat"]
    _report(f"{TEST[0]}, trained on the {TRAIN[0]} behind-seat trials only",
            confusion(test, test_on(behind, test)))


if __name__ == "__main__":
    main()
