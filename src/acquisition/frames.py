"""Parser for the binary PCM frames sent by firmware/vpu_pcm_stream in binary mode.

Frame, little-endian:

    sync u32 (0xA55A5AA5) | first_sample u32 | n u16 | ovf u16 | proc_us u16
    | n x int16 | crc16

crc16 is CRC-16/CCITT-FALSE over first_sample through the samples. Anything that is not
a valid frame (startup text, a frame cut short by a USB drop) is skipped by searching for
the next sync word.
"""

import binascii
import struct
from dataclasses import dataclass

import numpy as np

SYNC = struct.pack("<I", 0xA55A5AA5)
HEADER_SIZE = 14
MAX_SAMPLES = 1024
"""Largest n accepted; a larger value means a false sync match."""


@dataclass(frozen=True)
class Frame:
    first_sample: int
    samples: np.ndarray
    ovf: int
    proc_us: int
    host_time_s: float


class FrameParser:
    """Incremental parser: feed() raw bytes as they arrive, get complete valid frames."""

    def __init__(self):
        self._buf = bytearray()
        self.frames = 0
        self.crc_errors = 0
        self.skipped_bytes = 0
        """Bytes discarded while searching for a sync word, after the first valid frame."""
        self.leading_bytes = 0
        """Bytes discarded before the first valid frame (startup text, partial frame)."""

    def _skip(self, n: int) -> None:
        if self.frames:
            self.skipped_bytes += n
        else:
            self.leading_bytes += n
        del self._buf[:n]

    def feed(self, data: bytes, host_time_s: float) -> list[Frame]:
        self._buf += data
        out = []
        while True:
            i = self._buf.find(SYNC)
            if i < 0:
                self._skip(max(0, len(self._buf) - (len(SYNC) - 1)))
                return out
            if i:
                self._skip(i)
            if len(self._buf) < HEADER_SIZE:
                return out
            first, n, ovf, proc_us = struct.unpack_from("<IHHH", self._buf, 4)
            if n > MAX_SAMPLES:
                self._skip(1)
                continue
            size = HEADER_SIZE + 2 * n + 2
            if len(self._buf) < size:
                return out
            body = bytes(self._buf[4 : size - 2])
            (crc,) = struct.unpack_from("<H", self._buf, size - 2)
            if binascii.crc_hqx(body, 0xFFFF) != crc:
                self.crc_errors += 1
                self._skip(1)
                continue
            samples = np.frombuffer(body, dtype="<i2", offset=HEADER_SIZE - 4).copy()
            del self._buf[:size]
            self.frames += 1
            out.append(Frame(first, samples, ovf, proc_us, host_time_s))
