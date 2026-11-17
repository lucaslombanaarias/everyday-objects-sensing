"""models/toilet_ice_vs_pour.json against the code and trials it is made from.

The file is written by scripts/toilet_ice_vs_pour.py. These tests refit the model from
data/controlled/ and fail while the saved file differs, so a change to the features, the fit,
the tags or the trials cannot be committed without the file (run the script again), and the
file cannot be edited by hand.
"""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from everyday_sensing import event_classifier as ec

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "toilet_ice_vs_pour.py"
STALE = "models/toilet_ice_vs_pour.json is out of date: run python scripts/toilet_ice_vs_pour.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("toilet_ice_vs_pour", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sessions(script):
    return script.collect(*script.TRAIN), script.collect(*script.TEST)


def test_saved_model_is_the_one_the_code_fits_from_the_trials(script, sessions):
    train, _ = sessions
    fitted, saved = script.fit(train), ec.load_model(script.MODEL)
    for key in ("mean", "scale", "coef", "intercept"):
        np.testing.assert_allclose(saved[key], fitted[key], rtol=1e-3, atol=1e-4,
                                   err_msg=f"{key}: {STALE}")


def test_saved_model_gives_the_score_written_with_it(script, sessions):
    train, test = sessions
    x, is_ice = script._xy(test)
    said_ice = ec.predict_proba(ec.load_model(script.MODEL), x) > 0.5
    written = json.loads(script.MODEL.read_text(encoding="utf-8"))
    assert written["tested_on"] == {"session": script.TEST[0], "of": len(test),
                                    "correct": int((said_ice == is_ice).sum())}, STALE
    assert written["trained_on"] == script.about(train, {"correct": 0, "n": 0})["trained_on"], STALE


def test_model_round_trips_through_its_file(tmp_path):
    rng = np.random.default_rng(0)
    x = rng.normal(size=(40, len(ec.FEATURES)))
    model = ec.fit_logistic(x, x[:, 0] > 0)
    ec.save_model(model, tmp_path / "m.json", {"note": "x"})
    np.testing.assert_allclose(ec.predict_proba(ec.load_model(tmp_path / "m.json"), x),
                               ec.predict_proba(model, x), rtol=1e-8)


def test_load_refuses_a_file_saved_for_other_features(tmp_path):
    saved = {"features": ["peak_db"], "mean": [0], "scale": [1], "coef": [1], "intercept": 0}
    (tmp_path / "m.json").write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError):
        ec.load_model(tmp_path / "m.json")
