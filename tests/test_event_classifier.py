import numpy as np

from everyday_sensing import event_classifier as ec

FS = 48_000.0


def _level(n_s: float, floor_db: float = 0.0) -> np.ndarray:
    return np.full(int(n_s * ec.LEVEL_RATE_HZ), floor_db)


def test_logistic_separates_two_clusters_and_balances_classes():
    rng = np.random.default_rng(0)
    a = rng.normal([0.0, 0.0], 1.0, size=(200, 2))
    b = rng.normal([6.0, 6.0], 1.0, size=(20, 2))
    x, y = np.vstack([a, b]), np.r_[np.zeros(200), np.ones(20)]
    model = ec.fit_logistic(x, y)
    p = ec.predict_proba(model, x)
    assert ((p > 0.5) == y.astype(bool)).all()
    # Balanced classes: the midpoint between the clusters is undecided, not pulled to the
    # larger class.
    assert abs(ec.predict_proba(model, [[3.0, 3.0]])[0] - 0.5) < 0.2


def test_logistic_handles_a_constant_column():
    x = np.c_[np.r_[np.zeros(10), np.ones(10)], np.full(20, 7.0)]
    y = np.r_[np.zeros(10), np.ones(10)]
    p = ec.predict_proba(ec.fit_logistic(x, y), x)
    assert np.isfinite(p).all() and ((p > 0.5) == y.astype(bool)).all()


def test_an_impact_out_of_silence_rises_further_than_a_splash_in_a_pour():
    rng = np.random.default_rng(1)
    hp = rng.normal(0, 1.0, int(4 * FS))
    quiet = _level(4)
    quiet[1999:2010] = 40.0                      # one 10 ms impact at 2 s
    pour = _level(4, 25.0)
    pour[1999:2010] = 40.0                       # the same peak inside a steady pour
    alone = ec.event_features(hp, FS, quiet, 2.0)
    inside = ec.event_features(hp, FS, pour, 2.0)
    assert alone["peak_db"] == inside["peak_db"] == 40.0
    assert alone["rise_db"] == 40.0 and inside["rise_db"] == 15.0
    assert alone["context_db"] == -40.0 and inside["context_db"] == -15.0
    assert alone["decay_s"] == 0.011 and inside["decay_s"] == 0.18
    assert set(alone) == set(ec.FEATURES)


def test_context_sees_the_pour_that_follows_its_first_splash():
    level = _level(4)
    level[1999:] = 25.0                          # pour starts at 2 s
    level[1999:2010] = 40.0                      # with a splash
    hp = np.random.default_rng(2).normal(0, 1.0, int(4 * FS))
    f = ec.event_features(hp, FS, level, 2.0)
    assert f["rise_db"] == 40.0                  # out of silence, like an impact
    assert f["context_db"] == -15.0              # but the pour after it is seen


def test_spectral_shares_follow_the_frequency_of_the_event():
    t = np.arange(int(2 * FS)) / FS
    level = _level(2)
    low = ec.event_features(ec.highpassed(np.sin(2 * np.pi * 300 * t), FS), FS, level, 1.0)
    high = ec.event_features(ec.highpassed(np.sin(2 * np.pi * 4000 * t), FS), FS, level, 1.0)
    assert low["low_share_db"] > -0.1 and low["high_share_db"] < -40
    assert high["high_share_db"] > -0.1 and high["low_share_db"] < -40
    assert abs(10 ** low["centroid_log_hz"] - 300) < 15
    assert abs(10 ** high["centroid_log_hz"] - 4000) < 100


def test_measurable_needs_history_and_a_tail():
    level = _level(2)
    assert not ec.measurable(0.2, level)
    assert ec.measurable(1.0, level)
    assert not ec.measurable(1.9, level)
