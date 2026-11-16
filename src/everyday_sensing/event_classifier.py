"""Per-event features and a small logistic regression, for telling kinds of events apart.

Used by scripts/toilet_ice_vs_pour.py to separate ice drops from the splashes of a water pour.
Only numpy and scipy are needed.

An event is a det-v1 detection (everyday_sensing.impacts): a time, on the trace of the
detection level (5 ms RMS above 100 Hz, in dB above a noise baseline, 1000 values per second).
Its FEATURES are:

- peak_db: the detection level at the event.
- rise_db: the level at the event minus the median level over 0.30 to 0.05 s before it. An
  impact out of silence rises far; a splash in the middle of a pour does not.
- context_db: the higher of the median level over the 1 s before the event (to 0.05 s before)
  and over 0.3 to 1.3 s after it, minus the level at the event. It is near zero when there is
  sustained activity next to the event, so the first and last splash of a pour, which both
  rise out of silence, are still seen to belong to it.
- crest_db: the level at the event minus the median level from 20 ms before to 180 ms after.
- decay_s: the time for the level to fall 15 dB below the event's, up to 0.18 s.
- low_share_db and high_share_db: the share of the 100 Hz to 8 kHz power that is in 100 to
  500 Hz and in 2 to 8 kHz, in dB, from 20 ms before the event to 180 ms after.
- centroid_log_hz: log10 of the power-weighted geometric mean frequency over 100 Hz to 8 kHz.
"""

import numpy as np
from scipy import optimize, signal

FEATURES = ("peak_db", "low_share_db", "high_share_db", "centroid_log_hz", "crest_db",
            "decay_s", "rise_db", "context_db")
LEVEL_RATE_HZ = 1000
"""Rate of the detection-level trace the features index (impacts.DETECTION.rate_hz)."""
EDGE_S = 0.35
"""An event closer than this to the start of a recording has no history to measure."""
TAIL_S = 0.18
"""Length of trace needed after an event."""
NO_CONTEXT_DB = -60.0
"""context_db after the event when the recording ends too soon to measure it."""


def highpassed(x, fs: float) -> np.ndarray:
    """The recording above 100 Hz with no delay (zero-phase), for the spectral features."""
    sos = signal.butter(4, 100, "highpass", fs=fs, output="sos")
    return signal.sosfiltfilt(sos, np.asarray(x, dtype=np.float64))


def measurable(t_s: float, level_db) -> bool:
    """Whether an event at t_s has enough trace before and after it for event_features."""
    return t_s >= EDGE_S and t_s * LEVEL_RATE_HZ + TAIL_S * LEVEL_RATE_HZ < len(level_db)


def event_features(hp, fs: float, level_db, t_s: float) -> dict:
    """The FEATURES of the event at t_s. hp is highpassed(x, fs) and level_db the detection
    level of the same recording (impacts.detection_traces(...)["level_db"])."""
    level = np.asarray(level_db, dtype=np.float64)
    n, i = level.size, int(round(t_s * LEVEL_RATE_HZ)) - 1
    peak = level[i]
    below = np.nonzero(level[i:i + 180] < peak - 15)[0]
    before = np.median(level[max(0, i - 1000):max(1, i - 50)]) - peak
    after = (np.median(level[min(n - 1, i + 300):min(n, i + 1300)]) - peak
             if i + 400 < n else NO_CONTEXT_DB)
    start = max(0, int((t_s - 0.02) * fs))
    f, p = signal.welch(hp[start:start + int(0.2 * fs)], fs, nperseg=2048)
    band = (f >= 100) & (f < 8000)
    total = p[band].sum()
    return {
        "peak_db": float(peak),
        "low_share_db": float(10 * np.log10(p[(f >= 100) & (f < 500)].sum() / total)),
        "high_share_db": float(10 * np.log10(p[(f >= 2000) & (f < 8000)].sum() / total)),
        "centroid_log_hz": float(np.sum(np.log(f[band]) * p[band]) / total / np.log(10)),
        "crest_db": float(peak - np.median(level[max(0, i - 20):i + 180])),
        "decay_s": float(below[0] if below.size else 180) / LEVEL_RATE_HZ,
        "rise_db": float(peak - np.median(level[max(0, i - 300):max(1, i - 50)])),
        "context_db": float(max(before, after)),
    }


def fit_logistic(x, y, c: float = 1.0) -> dict:
    """Logistic regression of the labels y (0 or 1) on the rows of x.

    The columns are standardized (mean 0, standard deviation 1). The two classes are weighted
    so each counts the same in total whatever its size, and the coefficients (not the
    intercept) carry an L2 penalty of 1 / (2 c) times their squared length. Returns the model
    for predict_proba."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y).astype(bool)
    mean, scale = x.mean(axis=0), x.std(axis=0)
    scale = np.where(scale > 0, scale, 1.0)
    z = (x - mean) / scale
    sign = np.where(y, 1.0, -1.0)
    weight = np.where(y, y.size / (2 * y.sum()), y.size / (2 * (~y).sum()))

    def loss(theta):
        margin = sign * (z @ theta[:-1] + theta[-1])
        grad_margin = -weight * sign / (1 + np.exp(margin))
        value = np.sum(weight * np.logaddexp(0, -margin)) + 0.5 / c * theta[:-1] @ theta[:-1]
        return value, np.append(z.T @ grad_margin + theta[:-1] / c, grad_margin.sum())

    fit = optimize.minimize(loss, np.zeros(x.shape[1] + 1), jac=True, method="L-BFGS-B",
                            options={"maxiter": 2000, "ftol": 1e-12, "gtol": 1e-8})
    return {"mean": mean, "scale": scale, "coef": fit.x[:-1], "intercept": float(fit.x[-1])}


def predict_proba(model: dict, x) -> np.ndarray:
    """Probability of class 1 for each row of x, from a fit_logistic model."""
    z = (np.asarray(x, dtype=np.float64) - model["mean"]) / model["scale"]
    return 1 / (1 + np.exp(-(z @ model["coef"] + model["intercept"])))
