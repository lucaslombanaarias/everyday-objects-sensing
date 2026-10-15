"""Streaming band envelope and event detection for the VPU PCM stream (no Qt).

Used by vpu_pcm_dashboard.py. Same measure as the stomp check in
firmware/vpu_pcm_stream/README.md: band-pass the signal, take the RMS over a 50 ms
window, and call it an event when that envelope rises a set number of dB above the
median envelope of a quiet stretch (the noise baseline).
"""

import math

import numpy as np
from scipy import signal


class BandEnvelope:
    """Band-pass then moving RMS, output every `step` input samples.

    Feed consecutive chunks to process(); filter state and the RMS window carry across
    chunks, so the output does not depend on how the stream was split.
    """

    def __init__(self, fs, band=(100.0, 500.0), window_s=0.05, out_rate_hz=200.0, order=4):
        self.fs = float(fs)
        self.band = band
        lo, hi = band
        if hi is None or hi >= self.fs / 2:
            self.sos = signal.butter(order, lo, "highpass", fs=self.fs, output="sos")
        else:
            self.sos = signal.butter(order, [lo, hi], "bandpass", fs=self.fs, output="sos")
        self.zi = np.zeros((self.sos.shape[0], 2))
        self.win = max(1, round(window_s * self.fs))
        self.step = max(1, round(self.fs / out_rate_hz))
        self.rate = self.fs / self.step
        self._sq = np.zeros(0)
        self._count = 0
        self._next_end = math.ceil(self.win / self.step) * self.step

    def process(self, x) -> np.ndarray:
        y, self.zi = signal.sosfilt(self.sos, np.asarray(x, dtype=np.float64), zi=self.zi)
        sq = np.concatenate([self._sq, y * y])
        start = self._count - self._sq.size  # stream index of sq[0]
        self._count += y.size
        ends = np.arange(self._next_end, self._count + 1, self.step)
        out = np.zeros(0)
        if ends.size:
            cs = np.concatenate([[0.0], np.cumsum(sq)])
            e = ends - start
            out = np.sqrt(np.maximum(cs[e] - cs[e - self.win], 0.0) / self.win)
            self._next_end = int(ends[-1]) + self.step
        self._sq = sq[-self.win :]
        return out


class EventDetector:
    """Hysteresis detector on an envelope stream.

    An event starts when the envelope reaches threshold_db above the baseline (and at
    least min_gap_s after the previous event ended) and ends when it falls below
    release_db above the baseline. process() returns the events that ended.
    """

    def __init__(self, rate, threshold_db=20.0, release_db=14.0, min_gap_s=0.3):
        self.rate = float(rate)
        self.threshold_db = threshold_db
        self.release_db = release_db
        self.min_gap_s = min_gap_s
        self.baseline = None
        self.reset()

    def reset(self):
        self.index = 0
        self.in_event = False
        self.start = self.peak_index = 0
        self.peak = 0.0
        self.last_end = -math.inf

    def learn(self, envelope) -> float:
        """Set the baseline to the median of a quiet stretch of envelope values."""
        self.baseline = float(np.median(envelope))
        return self.baseline

    def level_db(self, envelope) -> np.ndarray:
        floor = np.finfo(np.float64).tiny
        return 20 * np.log10(np.maximum(envelope, floor) / max(self.baseline, floor))

    def process(self, envelope) -> list[dict]:
        events = []
        if self.baseline is None:
            self.index += len(envelope)
            return events
        on = self.baseline * 10 ** (self.threshold_db / 20)
        off = self.baseline * 10 ** (self.release_db / 20)
        for v in np.asarray(envelope, dtype=np.float64).tolist():
            i = self.index
            self.index += 1
            if not self.in_event:
                if v >= on and (i - self.last_end) / self.rate >= self.min_gap_s:
                    self.in_event, self.start, self.peak, self.peak_index = True, i, v, i
            else:
                if v > self.peak:
                    self.peak, self.peak_index = v, i
                if v < off:
                    self.in_event = False
                    self.last_end = i
                    events.append(
                        {
                            "t_s": self.peak_index / self.rate,
                            "peak": self.peak,
                            "snr_db": 20 * math.log10(self.peak / self.baseline),
                            "duration_s": (i - self.start) / self.rate,
                        }
                    )
        return events
