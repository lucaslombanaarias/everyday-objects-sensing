import binascii
import csv
import datetime
import json
import math
import struct
from pathlib import Path

import numpy as np
import pytest
import scipy.io

from acquisition.frames import SYNC, FrameParser
from acquisition.record import acquire, assemble, build_meta, parse_args, write_trial
from everyday_sensing.preprocess import FilterSpec, decimate

# 0.25 s of binary-mode output recorded from a XIAO ESP32-C6 running
# firmware/vpu_pcm_stream (sensor at rest), starting right after the 'B' command.
FIXTURE = Path(__file__).parent / "fixtures" / "vpu_pcm_frames.bin"
FS = 48000.0


@pytest.fixture(scope="module")
def raw():
    return FIXTURE.read_bytes()


def parse(data, chunk=None):
    p = FrameParser()
    frames = []
    step = chunk or len(data)
    for i in range(0, len(data), step):
        frames += p.feed(data[i : i + step], host_time_s=i / len(data))
    return p, frames


class FakeSerial:
    """Serves recorded bytes in fixed chunks and keeps what was written."""

    def __init__(self, data, chunk):
        self.data, self.chunk, self.pos, self.written = data, chunk, 0, b""

    def read(self, _size):
        out = self.data[self.pos : self.pos + self.chunk]
        self.pos += len(out)
        return out

    def write(self, b):
        self.written += b


def fake_clock(step):
    t = [0.0]

    def now():
        t[0] += step
        return t[0]

    return now


def frame_bytes(first, samples, ovf=0):
    body = struct.pack("<IHHH", first, len(samples), ovf, 0) + np.asarray(
        samples, dtype="<i2"
    ).tobytes()
    return SYNC + body + struct.pack("<H", binascii.crc_hqx(body, 0xFFFF))


def test_fixture_parses_cleanly(raw):
    p, frames = parse(raw)
    assert len(frames) > 50
    assert p.crc_errors == 0 and p.skipped_bytes == 0
    assert frames[0].first_sample == 0
    for a, b in zip(frames, frames[1:], strict=False):
        assert b.first_sample == a.first_sample + a.samples.size


def test_byte_by_byte_feeding_matches(raw):
    _, whole = parse(raw)
    _, single = parse(raw[:4000], chunk=1)
    n = len(single)
    assert n > 5
    for a, b in zip(whole[:n], single, strict=True):
        assert a.first_sample == b.first_sample
        np.testing.assert_array_equal(a.samples, b.samples)


def test_leading_text_is_skipped(raw):
    text = b"stats t_s=1.0 pdm_hz=3072000 rms=14.2\n# self-test\n"
    p, frames = parse(text + raw)
    _, clean = parse(raw)
    assert p.leading_bytes == len(text)
    assert len(frames) == len(clean)


def test_corrupt_frame_is_rejected_and_counted_as_gap(raw):
    _, frames = parse(raw)
    data = bytearray(raw)
    # Flip a sample byte inside the third frame.
    third = data.find(SYNC, data.find(SYNC, data.find(SYNC) + 1) + 1)
    data[third + 20] ^= 0xFF
    p, bad = parse(bytes(data))
    assert p.crc_errors == 1
    assert len(bad) == len(frames) - 1
    checks = assemble(bad, p, FS).checks
    assert checks["counter_gaps"] == 1
    assert checks["missing_samples"] == frames[2].samples.size
    assert not checks["ok"]


def test_clipping_and_overflow_counted():
    data = frame_bytes(0, [0, 32767, -32768, 5], ovf=7) + frame_bytes(4, [1, 2, 3, 4], ovf=9)
    p, frames = parse(data)
    checks = assemble(frames, p, FS).checks
    assert checks["clipped_samples"] == 2
    assert checks["device_overflow_events"] == 2


def test_acquire_and_write_trial(raw, tmp_path):
    # One 128-sample frame is 272 bytes; advancing the clock by 128 / fs per read makes
    # the fake port deliver at the real rate.
    ser = FakeSerial(raw, chunk=272)
    capture = acquire(ser, duration_s=0.3, nominal_fs_hz=FS, now=fake_clock(128 / FS))
    assert ser.written == b"BS"
    checks = capture.checks
    assert checks["crc_errors"] == 0 and checks["counter_gaps"] == 0
    assert checks["fs_within_tolerance"]
    assert checks["ok"]

    args = parse_args(
        ["--port", "COM3", "--trial-id", "t01", "--duration", "0.3", "--distance-m", "0.5",
         "--excitation-type", "drop", "--mass-g", "100", "--operator", "LL"]
    )
    start = datetime.datetime(2026, 10, 14, 22, 0, tzinfo=datetime.UTC)
    meta = build_meta(args, start)
    paths = write_trial(capture, meta, tmp_path)

    assert paths["csv"].parent == tmp_path / "2026-10-14"
    with paths["csv"].open() as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["sample_index", "host_time_s", "counts"]
    assert len(rows) - 1 == checks["samples"]
    assert [int(rows[1][0]), int(rows[1][2])] == [
        int(capture.sample_index[0]),
        int(capture.counts[0]),
    ]

    sidecar = json.loads(paths["json"].read_text())
    assert sidecar["trial_id"] == "t01"
    assert sidecar["distance_m"] == 0.5
    assert sidecar["excitation"] == {
        "type": "drop", "mass_g": 100.0, "drop_height_cm": None, "tip": None
    }
    assert sidecar["checks"] == checks
    assert sidecar["fs_hz"] == FS

    mat = scipy.io.loadmat(paths["mat"])
    assert mat["counts"].dtype == np.int16
    np.testing.assert_array_equal(mat["counts"].ravel(), capture.counts)
    assert float(mat["fs_hz"].squeeze()) == FS
    assert str(mat["meta"]["trial_id"][0, 0][0]) == "t01"

    with pytest.raises(FileExistsError):
        write_trial(capture, meta, tmp_path)


def test_noise_only_rejects_excitation_flags():
    base = ["--port", "COM3", "--trial-id", "n01", "--duration", "30", "--noise-only"]
    assert parse_args(base).noise_only
    with pytest.raises(SystemExit):
        parse_args(base + ["--mass-g", "100"])


@pytest.mark.parametrize("fs_in", [48000.0, 24000.0])
def test_preprocess_runs_on_recorded_counts(raw, fs_in):
    # The firmware's 24 kHz build produces the same int16 format at half the rate.
    p, frames = parse(raw)
    counts = assemble(frames, p, fs_in).counts
    spec = FilterSpec(fs_in=fs_in)
    y = decimate(counts, spec)
    assert len(y) == math.ceil(counts.size / spec.decimation_factor)
    assert np.all(np.isfinite(y))
