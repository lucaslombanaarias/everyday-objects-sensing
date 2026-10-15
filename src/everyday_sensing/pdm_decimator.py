"""PDM-to-PCM decimator for the VPU on the ESP32-C6, which has no hardware PDM2PCM filter.

The firmware in firmware/vpu_pcm_stream/ runs this exact integer chain on the raw 1-bit
stream; this module designs its filters, writes them to that sketch's pdm_filters.h, and
is the bit-exact reference model the tests check. Run as
``python -m everyday_sensing.pdm_decimator`` to regenerate the header and print the
measured responses.

All rates are relative to the PDM clock, so the same filters serve a 3.072 MHz clock
(48 kHz out) and a 1.536 MHz clock (24 kHz out):

1. Stage 1, CIC: order 5, decimation 16, run as an FIR on bytes through lookup tables.
   Its zeros sit on every multiple of the stage 1 output rate, which is where the
   noise-shaped PDM content would otherwise fold down.
2. Stage 2, FIR: linear-phase Kaiser low-pass, decimation 4, Q17 taps. Passband edge at
   fs_out / 4, stopband from fs_out / 2, so the PCM output band is alias-free.

Integer chain (one output sample per 64 PDM bits):

- bits b[k] in {0, 1}, time order; y1[m] = sum_j h1[j] * b[16 m + j] (0 to 2**20)
- x[m] = (2 * y1[m] - 2**20) >> 7, so PDM full scale is +-8192 (14 bits)
- acc[n] = sum_k h2[k] * x[4 n + k]; out[n] = clip((acc[n] + 2**14) >> 15) as int16,
  so PDM full scale is +-32768 again

Q15 taps cannot reach the 80 dB stopband (coefficient rounding sets a floor), so the taps
are Q17 and the stage 2 input is reduced to 14 bits to keep the accumulator in int32.
"""

import argparse
import functools
import math
from pathlib import Path

import numpy as np
from scipy import signal

CIC_ORDER = 5
CIC_DECIMATION = 16
FIR_DECIMATION = 4
DECIMATION = CIC_DECIMATION * FIR_DECIMATION
LUT_BYTES = 10
"""Bytes covered by the stage 1 window: the CIC has CIC_ORDER * 15 + 1 = 76 taps,
zero-padded to 80 bits."""
X_SHIFT = 7
"""Right shift from the stage 1 range (+-2**20 after mapping bits to +-1) to the stage 2
input range (+-2**13)."""
Q = 17
"""Stage 2 taps are integers scaled by 2**Q."""
OUT_SHIFT = 15
"""Right shift from the stage 2 accumulator (+-2**13 * 2**Q at DC full scale) to int16."""

FIR_PASSBAND_FRACTION = 0.25
"""Stage 2 passband edge as a fraction of the output rate."""
FIR_STOPBAND_FRACTION = 0.5
"""Stage 2 stopband edge as a fraction of the output rate (the output Nyquist)."""
FIR_STOPBAND_ATTEN_DB = 80.0
FIR_PASSBAND_RIPPLE_DB = 0.1
FREQZ_POINTS = 2**16 + 1

HEADER_PATH = (
    Path(__file__).resolve().parents[2] / "firmware" / "vpu_pcm_stream" / "pdm_filters.h"
)


def cic_taps() -> np.ndarray:
    """Stage 1 impulse response: CIC_ORDER boxcars of CIC_DECIMATION ones, padded to 80."""
    h = np.ones(1, dtype=np.int64)
    for _ in range(CIC_ORDER):
        h = np.convolve(h, np.ones(CIC_DECIMATION, dtype=np.int64))
    padded = np.zeros(8 * LUT_BYTES, dtype=np.int64)
    padded[: h.size] = h
    return padded


def cic_lut() -> np.ndarray:
    """LUT[p][v]: stage 1 contribution of byte value v at byte position p of the window.

    Bit 7 of each byte is the earliest bit in time.
    """
    h = cic_taps().reshape(LUT_BYTES, 8)
    bits = (np.arange(256)[:, None] >> (7 - np.arange(8))[None, :]) & 1
    return h @ bits.T


def _fir_response(taps: np.ndarray, fs_in: float) -> tuple[np.ndarray, np.ndarray]:
    freqs, h = signal.freqz(taps, worN=FREQZ_POINTS, fs=fs_in, include_nyquist=True)
    return freqs, 20 * np.log10(np.maximum(np.abs(h), np.finfo(np.float64).tiny))


def _fir_meets_spec(taps: np.ndarray) -> dict:
    """Measured passband ripple and stopband attenuation, rates normalized to fs_out = 1."""
    fs_in = float(FIR_DECIMATION)
    freqs, gain_db = _fir_response(taps, fs_in)
    return {
        "numtaps": len(taps),
        "passband_ripple_db": float(np.max(np.abs(gain_db[freqs <= FIR_PASSBAND_FRACTION]))),
        "stopband_atten_db": float(-np.max(gain_db[freqs >= FIR_STOPBAND_FRACTION])),
    }


@functools.cache
def fir_taps_q() -> np.ndarray:
    """Stage 2 taps as Q15 integers, the shortest Kaiser design that meets the spec after
    quantization. Raises ValueError if none does within twice the kaiserord estimate."""
    fs_in = float(FIR_DECIMATION)
    ripple_delta = 10 ** (FIR_PASSBAND_RIPPLE_DB / 20) - 1
    atten_db = max(FIR_STOPBAND_ATTEN_DB, -20 * math.log10(ripple_delta))
    width = (FIR_STOPBAND_FRACTION - FIR_PASSBAND_FRACTION) / (fs_in / 2)
    numtaps, beta = signal.kaiserord(atten_db, width)
    cutoff = (FIR_PASSBAND_FRACTION + FIR_STOPBAND_FRACTION) / 2
    for n in range(numtaps, 2 * numtaps + 1):
        taps = signal.firwin(n, cutoff, window=("kaiser", beta), fs=fs_in)
        q = np.round(taps * 2**Q).astype(np.int64)
        response = _fir_meets_spec(q / 2**Q)
        if (
            response["passband_ripple_db"] <= FIR_PASSBAND_RIPPLE_DB
            and response["stopband_atten_db"] >= FIR_STOPBAND_ATTEN_DB
        ):
            # Worst-case |acc| is sum|h| * 2**13; it must fit in int32 with the rounding term.
            if np.sum(np.abs(q)) * 2**13 + 2 ** (OUT_SHIFT - 1) >= 2**31:
                raise ValueError("stage 2 accumulator could overflow int32")
            q.setflags(write=False)
            return q
    raise ValueError("stage 2 spec not met after quantization")


def words_to_bytes(words: np.ndarray, msb_first: bool = True, dedupe: bool = False):
    """Time-ordered bytes from 16-bit DMA words, for a given layout.

    msb_first: bit 15 of each word is the earliest bit. Otherwise bit 0 is, so each byte
    is bit-reversed and the low byte comes first. dedupe: keep only words 0, 2, 4, ...;
    on the ESP32-C6 in mono raw PDM mode every word arrives twice.
    """
    w = np.asarray(words, dtype=np.uint16)
    if dedupe:
        w = w[0::2]
    hi, lo = (w >> 8).astype(np.uint8), (w & 0xFF).astype(np.uint8)
    if msb_first:
        return np.column_stack([hi, lo]).ravel()
    rev = np.array([int(f"{v:08b}"[::-1], 2) for v in range(256)], dtype=np.uint8)
    return np.column_stack([rev[lo], rev[hi]]).ravel()


def bits_to_bytes(bits: np.ndarray) -> np.ndarray:
    """Pack time-ordered bits into bytes, earliest bit in bit 7."""
    return np.packbits(np.asarray(bits, dtype=np.uint8))


def decimate_bytes(stream: np.ndarray) -> np.ndarray:
    """Run the integer chain on time-ordered PDM bytes; returns int16 PCM.

    Output n uses PDM bits 64 n to 64 n + 79 + 16 * (len(h2) - 1); there is no padding,
    so the output starts once the first full window is available.
    """
    b = np.unpackbits(np.asarray(stream, dtype=np.uint8)).astype(np.int64)
    h1 = cic_taps()
    y1 = np.convolve(b, h1[::-1], mode="valid")[::CIC_DECIMATION]
    x = (2 * y1 - 2**20) >> X_SHIFT
    h2 = fir_taps_q()
    acc = np.convolve(x, h2[::-1], mode="valid")[::FIR_DECIMATION]
    return np.clip((acc + 2 ** (OUT_SHIFT - 1)) >> OUT_SHIFT, -32768, 32767).astype(np.int16)


def chain_response(freqs_over_fs_out: np.ndarray) -> np.ndarray:
    """Overall gain (linear) of stage 1 then stage 2 at frequencies given relative to fs_out."""
    f = np.asarray(freqs_over_fs_out, dtype=np.float64)
    pdm_rate = float(DECIMATION)
    _, h1 = signal.freqz(cic_taps(), worN=f, fs=pdm_rate)
    _, h2 = signal.freqz(fir_taps_q() / 2**Q, worN=f, fs=float(FIR_DECIMATION))
    return np.abs(h1) / 2**20 * np.abs(h2)


def summary() -> dict:
    """Measured figures of the design, rates relative to fs_out."""
    fir = _fir_meets_spec(fir_taps_q() / 2**Q)
    # Stage 1 aliasing: worst gain over the bands that fold onto the output band
    # [0, fs_out / 2] at the stage 1 decimation, i.e. k * 4 +- 0.5 (k = 1 .. 7).
    stage1_rate = float(FIR_DECIMATION)
    grid = np.linspace(-0.5, 0.5, 2001)
    alias = np.concatenate([k * stage1_rate + grid for k in range(1, CIC_DECIMATION // 2)])
    _, h1 = signal.freqz(cic_taps(), worN=alias, fs=float(DECIMATION))
    _, h1_dc = signal.freqz(cic_taps(), worN=[0.0], fs=float(DECIMATION))
    droop = chain_response(np.array([FIR_PASSBAND_FRACTION]))[0] / chain_response(
        np.array([0.0])
    )[0]
    return {
        "cic_order": CIC_ORDER,
        "decimation": DECIMATION,
        "stage2_numtaps": fir["numtaps"],
        "stage2_passband_ripple_db": fir["passband_ripple_db"],
        "stage2_stopband_atten_db": fir["stopband_atten_db"],
        "stage1_min_alias_atten_db": float(
            -20 * np.log10(np.max(np.abs(h1)) / np.abs(h1_dc[0]))
        ),
        "cic_droop_at_passband_edge_db": float(20 * np.log10(droop)),
    }


def header_text() -> str:
    lut = cic_lut()
    h2 = fir_taps_q()
    s = summary()
    lines = [
        "// Generated by python -m everyday_sensing.pdm_decimator. Do not edit by hand.",
        "// Stage 1: CIC order 5, decimation 16, as byte lookup tables (bit 7 = earliest).",
        f"// Stage 2: {len(h2)}-tap Q{Q} FIR, decimation 4. Measured: passband ripple "
        f"{s['stage2_passband_ripple_db']:.3g} dB, stopband attenuation "
        f"{s['stage2_stopband_atten_db']:.2f} dB.",
        "#pragma once",
        "#include <stdint.h>",
        "",
        f"#define PDM_LUT_BYTES {LUT_BYTES}",
        f"#define PDM_CIC_FULL_SCALE {2**20}",
        f"#define PDM_X_SHIFT {X_SHIFT}",
        f"#define PDM_FIR_TAPS {len(h2)}",
        f"#define PDM_FIR_Q {Q}",
        f"#define PDM_OUT_SHIFT {OUT_SHIFT}",
        "",
        "static const int32_t PDM_LUT[PDM_LUT_BYTES][256] = {",
    ]
    for row in lut:
        lines.append("  {" + ", ".join(str(int(v)) for v in row) + "},")
    lines += ["};", "", "static const int32_t PDM_FIR[PDM_FIR_TAPS] = {"]
    lines.append("  " + ", ".join(str(int(v)) for v in h2))
    lines += ["};", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--header", type=Path, default=HEADER_PATH)
    args = parser.parse_args(argv)
    args.header.parent.mkdir(parents=True, exist_ok=True)
    args.header.write_text(header_text(), encoding="ascii", newline="\n")
    for key, value in summary().items():
        print(f"{key}: {value}")
    print(f"wrote {args.header}")


if __name__ == "__main__":
    main()
