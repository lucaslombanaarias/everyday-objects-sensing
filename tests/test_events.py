import numpy as np
import pytest

from everyday_sensing.events import BandEnvelope, EventDetector

FS = 48000.0


def bursts(times_s, duration_s, amplitude=200.0, freq=300.0, tau=0.03, noise=2.0, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration_s * FS)) / FS
    x = noise * rng.standard_normal(t.size)
    for t0 in times_s:
        dt = t - t0
        x += np.where(dt >= 0, amplitude * np.exp(-dt / tau) * np.sin(2 * np.pi * freq * dt), 0)
    return x


def run(x, chunk=4800, **kw):
    env = BandEnvelope(FS)
    det = EventDetector(env.rate, **kw)
    det.learn(env.process(x[: int(FS)]))  # first second is quiet in these signals
    events = []
    for i in range(int(FS), x.size, chunk):
        events += det.process(env.process(x[i : i + chunk]))
    return env, det, events


def test_envelope_independent_of_chunking():
    x = bursts([0.5, 1.2], 2.0)
    whole = BandEnvelope(FS).process(x)
    env = BandEnvelope(FS)
    rng = np.random.default_rng(1)
    parts, i = [], 0
    while i < x.size:
        n = int(rng.integers(1, 5000))
        parts.append(env.process(x[i : i + n]))
        i += n
    np.testing.assert_allclose(np.concatenate(parts), whole, rtol=1e-9, atol=1e-12)


def test_envelope_of_in_band_tone_is_its_rms():
    t = np.arange(int(2 * FS)) / FS
    env = BandEnvelope(FS, band=(100.0, 500.0)).process(10 * np.sin(2 * np.pi * 300 * t))
    np.testing.assert_allclose(env[-50:], 10 / np.sqrt(2), rtol=0.01)


def test_out_of_band_hum_is_rejected():
    # The 41 Hz hum measured at rest must not count as band energy.
    t = np.arange(int(2 * FS)) / FS
    env = BandEnvelope(FS, band=(100.0, 500.0)).process(30 * np.sin(2 * np.pi * 41 * t))
    assert env[-50:].max() < 0.05 * 30 / np.sqrt(2)


def test_detects_each_burst_at_its_time():
    times = [1.5, 2.1, 2.7, 3.6]
    _, det, events = run(bursts(times, 5.0))
    assert len(events) == len(times)
    for ev, t0 in zip(events, times, strict=True):
        assert ev["t_s"] + 1.0 == pytest.approx(t0, abs=0.06)  # detector starts at 1 s
        assert ev["snr_db"] >= det.threshold_db


def test_quiet_signal_has_no_events():
    _, _, events = run(bursts([], 5.0))
    assert events == []


def test_min_gap_merges_close_bursts():
    # Two bursts 0.15 s apart with a 0.3 s minimum gap count once.
    _, _, events = run(bursts([2.0, 2.15], 4.0), min_gap_s=0.3)
    assert len(events) == 1


def test_no_events_before_baseline():
    env = BandEnvelope(FS)
    det = EventDetector(env.rate)
    assert det.process(env.process(bursts([0.5], 1.0))) == []
