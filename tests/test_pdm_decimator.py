import numpy as np
import pytest

from everyday_sensing.pdm_decimator import (
    DECIMATION,
    HEADER_PATH,
    bits_to_bytes,
    chain_response,
    decimate_bytes,
    fir_taps_q,
    header_text,
    summary,
    words_to_bytes,
)

# Rates are relative to the output rate (fs_out = 1, PDM clock = DECIMATION).
N_OUT = 2000
AMPLITUDE = 0.5


def sigma_delta(x):
    """Second-order sigma-delta modulator: samples in [-1, 1] to bits in {0, 1}."""
    bits = np.empty(x.size, dtype=np.uint8)
    i1 = i2 = 0.0
    y = 1.0
    for k, v in enumerate(x.tolist()):
        i1 += v - y
        i2 += i1 - y
        y = 1.0 if i2 >= 0 else -1.0
        bits[k] = y > 0
    return bits


def tone_bits(freq, n_out=N_OUT, amplitude=AMPLITUDE):
    t = np.arange(n_out * DECIMATION) / DECIMATION
    return sigma_delta(amplitude * np.sin(2 * np.pi * freq * t))


def fit_tone(y, freq):
    """Least-squares amplitude of a tone at freq (cycles per output sample) and residual RMS."""
    n = np.arange(y.size)
    basis = np.column_stack([np.cos(2 * np.pi * freq * n), np.sin(2 * np.pi * freq * n)])
    coef, *_ = np.linalg.lstsq(basis, y, rcond=None)
    return float(np.hypot(*coef)), float(np.sqrt(np.mean((y - basis @ coef) ** 2)))


def to_words(stream):
    """16-bit words as the DMA would hold them for MSB-first bit order."""
    b = np.asarray(stream, dtype=np.uint16)
    return (b[0::2] << 8) | b[1::2]


def pcm(bits):
    return decimate_bytes(bits_to_bytes(bits)).astype(np.float64) / 32768


@pytest.fixture(scope="module")
def tone_2k4():
    # 0.05 * fs_out: 2.4 kHz at 48 kHz out, inside the 0.25 * fs_out passband.
    return tone_bits(0.05)


def test_header_matches_design():
    assert HEADER_PATH.read_text(encoding="ascii") == header_text()


def test_design_meets_spec():
    s = summary()
    assert s["stage2_stopband_atten_db"] >= 80
    assert s["stage2_passband_ripple_db"] <= 0.1
    np.testing.assert_array_equal(fir_taps_q(), fir_taps_q()[::-1])


def test_full_scale_maps_to_int16_limits():
    n_bytes = 64 * 8
    assert np.all(decimate_bytes(np.full(n_bytes, 0xFF, dtype=np.uint8)) == 32767)
    assert np.all(decimate_bytes(np.zeros(n_bytes, dtype=np.uint8)) == -32768)


def test_passband_tone_recovered(tone_2k4):
    y = pcm(tone_2k4)
    amplitude, residual = fit_tone(y, 0.05)
    expected = AMPLITUDE * chain_response(np.array([0.05]))[0]
    assert amplitude == pytest.approx(expected, rel=1e-3)
    assert 20 * np.log10(residual / amplitude) < -60


def test_stopband_tone_suppressed():
    # 0.9 * fs_out would alias to 0.1 * fs_out; it must not appear there.
    y = pcm(tone_bits(0.9))
    alias, _ = fit_tone(y, 0.1)
    assert 20 * np.log10(alias / AMPLITUDE) < -70


def test_bit_order_round_trip(tone_2k4):
    stream = bits_to_bytes(tone_2k4)
    words = to_words(stream)
    np.testing.assert_array_equal(words_to_bytes(words, msb_first=True), stream)

    rev = np.array([int(f"{v:08b}"[::-1], 2) for v in range(256)], dtype=np.uint16)
    lsb_words = (rev[stream[1::2]] << 8) | rev[stream[0::2]]
    np.testing.assert_array_equal(words_to_bytes(lsb_words, msb_first=False), stream)

    doubled = np.repeat(words, 2)
    np.testing.assert_array_equal(words_to_bytes(doubled, dedupe=True), stream)


@pytest.mark.parametrize("msb_first, dedupe", [(True, False), (False, True), (False, False)])
def test_wrong_word_layout_raises_noise(tone_2k4, msb_first, dedupe):
    # The firmware's self-test picks the layout with the lowest noise; this checks that a
    # wrong layout is clearly noisier than the right one on a known signal. The words are
    # doubled as the C6 delivers them, so the right layout is MSB-first with dedupe.
    words = np.repeat(to_words(bits_to_bytes(tone_2k4)), 2)
    right = decimate_bytes(words_to_bytes(words, dedupe=True)).astype(np.float64) / 32768
    wrong = decimate_bytes(words_to_bytes(words, msb_first, dedupe)).astype(np.float64)
    _, right_residual = fit_tone(right, 0.05)
    _, wrong_residual = fit_tone(wrong / 32768, 0.05)
    assert wrong_residual > 10 * right_residual
