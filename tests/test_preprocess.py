import math

import numpy as np
import pytest

from everyday_sensing.preprocess import (
    FilterSpec,
    decimate,
    design_decimation_filter,
    edge_samples,
    kaiser_estimate,
    measured_response,
    split_on_gaps,
)

DURATION_S = 2.0
AMPLITUDE = 0.5
# For a tone that folds exactly to 0 Hz (4,500 Hz at a 1,500 Hz output), every output
# sample sees the same phase phi, so output RMS / input RMS = sqrt(2) * |cos(phi)| * |H(f)|.
# phi = pi/4 makes that factor exactly 1; tones that fold elsewhere give |H(f)| for any
# phi. The RMS ratio therefore equals |H(f)| and is bounded by the spec with no margin.
PHASE = math.pi / 4

RESONANCE_TONES_HZ = [4000.0, 4500.0, 5000.0]
PASSBAND_TONES_HZ = [50.0, 100.0, 250.0, 450.0]


@pytest.fixture(scope="module", params=[48000.0, 24000.0], ids=["48k", "24k"])
def design(request):
    spec = FilterSpec(fs_in=request.param)
    return spec, design_decimation_filter(spec)


@pytest.fixture(scope="module")
def design_48k():
    spec = FilterSpec(fs_in=48000.0)
    return spec, design_decimation_filter(spec)


def tone(freq_hz, fs, duration_s=DURATION_S):
    t = np.arange(round(duration_s * fs)) / fs
    return AMPLITUDE * np.cos(2 * np.pi * freq_hz * t + PHASE)


def rms(x):
    return np.sqrt(np.mean(np.square(x)))


def trim(y, k):
    return y[k : len(y) - k]


@pytest.mark.parametrize("freq_hz", RESONANCE_TONES_HZ + [750.0])
def test_stopband_tones_attenuated(design, freq_hz):
    # 750 Hz is the stopband edge, where the attenuation is closest to the spec.
    spec, taps = design
    x = tone(freq_hz, spec.fs_in)
    y = trim(decimate(x, spec, taps), edge_samples(taps, spec))
    ratio_db = 20 * np.log10(rms(y) / rms(x))
    assert ratio_db <= -spec.stopband_atten_db


def test_naive_decimation_aliases_resonance(design):
    # Negative control: without the filter the 4,500 Hz tone folds to 0 Hz unattenuated
    # (RMS ratio exactly 1 by the PHASE choice above), so the stopband test can see aliasing.
    spec, _ = design
    x = tone(4500.0, spec.fs_in)
    naive = x[:: spec.decimation_factor]
    assert rms(naive) / rms(x) == pytest.approx(1.0, rel=1e-9)


@pytest.mark.parametrize("freq_hz", PASSBAND_TONES_HZ)
def test_passband_tones_preserved(design, freq_hz):
    spec, taps = design
    k = edge_samples(taps, spec)
    y = trim(decimate(tone(freq_hz, spec.fs_in), spec, taps), k)
    t_out = (np.arange(len(y)) + k) / spec.fs_out

    # Least-squares amplitude of the tone at freq_hz in the output.
    phase = 2 * np.pi * freq_hz * t_out
    basis = np.column_stack([np.cos(phase), np.sin(phase)])
    coef, *_ = np.linalg.lstsq(basis, y, rcond=None)
    gain_db = 20 * np.log10(np.hypot(*coef) / AMPLITUDE)
    assert abs(gain_db) <= spec.passband_ripple_db

    spectrum = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    freqs = np.fft.rfftfreq(len(y), d=1 / spec.fs_out)
    bin_hz = spec.fs_out / len(y)
    assert abs(freqs[np.argmax(spectrum)] - freq_hz) <= bin_hz / 2


def test_time_alignment(design_48k):
    spec, taps = design_48k
    k = edge_samples(taps, spec)
    freq_hz = 100.0
    y = trim(decimate(tone(freq_hz, spec.fs_in), spec, taps), k)
    t_out = (np.arange(len(y)) + k) / spec.fs_out
    expected = AMPLITUDE * np.cos(2 * np.pi * freq_hz * t_out + PHASE)
    # With zero net delay the only error is the passband gain deviation, bounded by the
    # measured ripple. A one-input-sample shift would be far larger than this.
    max_gain_error = 10 ** (measured_response(taps, spec)["passband_ripple_db"] / 20) - 1
    np.testing.assert_allclose(y, expected, rtol=0, atol=AMPLITUDE * max_gain_error + 1e-12)


@pytest.mark.parametrize("n_in", [48000, 48001, 48031, 47999, 96032])
def test_output_length(design_48k, n_in):
    spec, taps = design_48k
    x = np.zeros(n_in)
    assert len(decimate(x, spec, taps)) == math.ceil(n_in / spec.decimation_factor)


def test_int16_input_scaled(design_48k):
    spec, taps = design_48k
    x = np.round(tone(100.0, spec.fs_in) * 32767).astype(np.int16)
    np.testing.assert_array_equal(
        decimate(x, spec, taps), decimate(x.astype(np.float64) / 32768, spec, taps)
    )


def test_default_taps_match_design(design_48k):
    spec, taps = design_48k
    x = tone(100.0, spec.fs_in, duration_s=0.5)
    np.testing.assert_array_equal(decimate(x, spec), decimate(x, spec, taps))


def test_design_is_cached_and_read_only(design_48k):
    spec, taps = design_48k
    assert design_decimation_filter(FilterSpec(fs_in=48000.0)) is taps
    with pytest.raises(ValueError):
        taps[0] = 1.0


def test_decimation_factors():
    assert FilterSpec(fs_in=48000).decimation_factor == 32
    assert FilterSpec(fs_in=24000).decimation_factor == 16


def test_non_integer_factor_raises():
    with pytest.raises(ValueError):
        FilterSpec(fs_in=48000, fs_out=1280)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fs_in": 0},
        {"fs_in": 1500},
        {"fs_in": 48000, "passband_edge_hz": 0},
        {"fs_in": 48000, "passband_edge_hz": 800},
        {"fs_in": 48000, "stopband_edge_hz": 1600},
        # Above fs_out - passband_edge_hz, so transition-band content folds into the passband.
        {"fs_in": 48000, "stopband_edge_hz": 1001},
        {"fs_in": 48000, "stopband_atten_db": -96},
        {"fs_in": 48000, "passband_ripple_db": 0},
    ],
)
def test_invalid_spec_raises(kwargs):
    with pytest.raises(ValueError):
        FilterSpec(**kwargs)


def test_invalid_input_raises(design_48k):
    spec, taps = design_48k
    with pytest.raises(ValueError):
        decimate(np.zeros((2, 100)), spec, taps)
    with pytest.raises(TypeError):
        decimate(np.zeros(100, dtype=np.int32), spec, taps)
    with pytest.raises(ValueError):
        decimate(np.zeros(100), spec, taps[:-1])
    with pytest.raises(ValueError):
        decimate(np.zeros(100), spec, np.arange(5.0))


def test_split_on_gaps_no_gap():
    t = np.arange(10) / 100
    assert split_on_gaps(t, 0.015) == [slice(0, 10)]


def test_split_on_gaps_one_gap():
    t = np.concatenate([np.arange(5), np.arange(5) + 10]) / 100
    assert split_on_gaps(t, 0.015) == [slice(0, 5), slice(5, 10)]


def test_split_on_gaps_at_start():
    t = np.concatenate([[0.0], 1 + np.arange(9) / 100])
    assert split_on_gaps(t, 0.015) == [slice(0, 1), slice(1, 10)]


def test_split_on_gaps_at_end():
    t = np.concatenate([np.arange(9) / 100, [1.0]])
    assert split_on_gaps(t, 0.015) == [slice(0, 9), slice(9, 10)]


def test_split_on_gaps_edge_cases():
    assert split_on_gaps(np.array([]), 0.015) == []
    with pytest.raises(ValueError):
        split_on_gaps(np.array([0.0, 0.02, 0.01]), 0.015)
    with pytest.raises(ValueError):
        split_on_gaps(np.arange(3.0), 0)
    with pytest.raises(ValueError):
        split_on_gaps(np.array([0.0, 0.01, np.nan, 0.03]), 0.015)


def test_designed_filter_meets_spec(design):
    spec, taps = design
    response = measured_response(taps, spec)
    assert response["numtaps"] == len(taps)
    assert len(taps) % 2 == 1
    np.testing.assert_array_equal(taps, taps[::-1])
    assert len(taps) >= kaiser_estimate(spec)[0]
    assert response["passband_ripple_db"] <= spec.passband_ripple_db
    assert response["stopband_atten_db"] >= spec.stopband_atten_db
