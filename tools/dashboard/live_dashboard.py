"""Live sensor dashboard: time series + PSD + spectrogram + peak-frequency readout.

Reads plain-text numeric lines from a serial port -- the same thing the Arduino
Serial Monitor shows. Accepted line formats (one sample per line):

    0.01,0.02,0.98          comma / space / tab / semicolon separated
    ax:0.01 ay:0.02 az:0.98 Arduino Serial Plotter style labels
    123456,0.01,0.02,0.98   with --time-col 0 the first column is a timestamp

Lines that are not purely numeric (boot messages, prints) are skipped.

Examples:
    python live_dashboard.py --preset tremor --port COM5
    python live_dashboard.py --preset vpu --port COM7 --baud 921600
    python live_dashboard.py --preset tremor --port SIM     # no hardware needed
    python live_dashboard.py --list                         # show serial ports

Run one window per board. Close the Arduino Serial Monitor/Plotter first --
only one program can hold a COM port.

Keys: Space = pause display, R = start/stop CSV recording.
"""

import argparse
import collections
import csv
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import serial
import serial.tools.list_ports
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

SIM = "SIM"

PRESETS = {
    # Wrist IMU on a cup. Tested on a recording of 3 drinks + 3 simulated tremors (gyro):
    # drinking keeps >90 % of its oscillation power below 2 Hz (band ratio <= 0.46,
    # amplitude <= 11 dps); shaking peaked at 7.5-9.5 Hz (ratio 0.70-0.86, 24-39 dps).
    # Pathological tremor spans ~4-12 Hz, so the band covers rest and essential tremor.
    # fmin 0.5 keeps slow drinking motion in the ratio's denominator.
    "tremor": dict(band=(4.0, 12.0), fmin=0.5, fmax=20.0, seg=1.5, psd_seconds=3.0,
                   window=8.0, ratio=0.6, min_amp=8.0, sim_fs=100.0, label="TREMOR"),
    # Voice pickup / vibration: audio-rate signal, analyse up to Nyquist by default.
    "vpu": dict(band=(80.0, 300.0), fmin=20.0, fmax=0.0, seg=0.25, psd_seconds=2.0,
                window=3.0, ratio=0.35, min_amp=0.0, sim_fs=2000.0, label="IN BAND"),
}

COLORS = ["#4fc3f7", "#ffb74d", "#81c784", "#e57373", "#ba68c8", "#fff176", "#4db6ac", "#f06292"]
SPEC_COLS = 300          # spectrogram history columns
SPEC_ROWS = 256          # spectrogram frequency rows
ANALYSIS_MS = 100        # PSD / spectrogram update period
PLOT_MS = 33             # time-series update period


# --------------------------------------------------------------------------- parsing

_SPLIT = re.compile(r"[,;\s]+")
_COLON = re.compile(r"\s*[:=]\s*")


def parse_line(text):
    """'1,2,3' / '1 2 3' / 'ax:1 ay:2' -> (labels, values), or None if not numeric."""
    text = _COLON.sub(":", text.strip())
    if not text:
        return None
    labels, values = [], []
    for tok in _SPLIT.split(text):
        if not tok:
            continue
        label, _, num = tok.rpartition(":")
        try:
            v = float(num)
        except ValueError:
            return None
        if not np.isfinite(v):
            return None
        labels.append(label)
        values.append(v)
    return (labels, values) if values else None


class Stream:
    """Thread-safe ring buffer of parsed samples (rows x channels) plus link stats."""

    CAP = 1 << 18

    def __init__(self):
        self.lock = threading.Lock()
        self.recorder = None
        self.reset()

    def reset(self):
        with self.lock:
            self.data = None
            self.total = 0
            self.labels = []
            self.bytes = 0
            self.good = 0
            self.bad = 0
            self.last_bad = ""
            self.status = "idle"
            self._pending_n = None
            self._pending_count = 0

    def feed(self, raw_lines):
        with self.lock:
            batch = []
            for raw in raw_lines:
                text = raw.decode("ascii", "ignore") if isinstance(raw, bytes) else raw
                parsed = parse_line(text)
                if parsed is None:
                    if text.strip():
                        self.bad += 1
                        self.last_bad = text.strip()[:80]
                    continue
                labels, values = parsed
                n = len(values)
                if self.data is None or n != self.data.shape[1]:
                    # Switch channel count only once it is consistent (ignores torn lines).
                    if n == self._pending_n:
                        self._pending_count += 1
                    else:
                        self._pending_n, self._pending_count = n, 1
                    if self.data is not None and self._pending_count < 10:
                        self.bad += 1
                        continue
                    self._push(batch)
                    batch = []
                    self.data = np.zeros((self.CAP, n))
                    self.total = 0
                    self.labels = [lab or f"ch{i + 1}" for i, lab in enumerate(labels)]
                self._pending_count = 0
                batch.append(values)
                self.good += 1
            self._push(batch)
            if self.recorder is not None and batch:
                now = time.time()
                self.recorder.writerows([now, *row] for row in batch)

    def _push(self, batch):
        if not batch:
            return
        rows = np.asarray(batch, dtype=float)[-self.CAP:]
        k = len(rows)
        i = self.total % self.CAP
        first = min(k, self.CAP - i)
        self.data[i:i + first] = rows[:first]
        self.data[:k - first] = rows[first:]
        self.total += k

    def snapshot(self, m):
        with self.lock:
            if self.data is None:
                return None, 0, []
            m = int(min(m, self.total, self.CAP))
            idx = np.arange(self.total - m, self.total) % self.CAP
            return self.data[idx].copy(), self.total, list(self.labels)


# --------------------------------------------------------------------------- sources

class SerialSource(threading.Thread):
    """Reads lines from a serial port; reconnects automatically if unplugged."""

    def __init__(self, port, baud, stream):
        super().__init__(daemon=True)
        self.port, self.baud, self.stream = port, baud, stream
        self.stop_evt = threading.Event()

    def run(self):
        while not self.stop_evt.is_set():
            try:
                ser = serial.Serial()
                ser.port, ser.baudrate, ser.timeout = self.port, self.baud, 0.05
                # RTS held high keeps ESP32-C3/C6 native-USB boards in reset.
                ser.dtr, ser.rts = True, False
                ser.open()
            except (serial.SerialException, OSError) as e:
                hint = " (close Arduino Serial Monitor?)" if "denied" in str(e).lower() else ""
                self.stream.status = f"{self.port}: cannot open{hint} - retrying"
                self.stop_evt.wait(1.0)
                continue
            self.stream.status = f"{self.port} @ {self.baud}"
            buf, first = b"", True
            try:
                while not self.stop_evt.is_set():
                    chunk = ser.read(max(1, ser.in_waiting))
                    if not chunk:
                        continue
                    self.stream.bytes += len(chunk)
                    buf += chunk
                    if b"\n" not in chunk:
                        if len(buf) > 65536:  # no newlines at all: wrong baud / binary data
                            buf = b""
                        continue
                    lines = buf.split(b"\n")
                    buf = lines.pop()
                    if first:  # first line is usually torn
                        lines, first = lines[1:], False
                    self.stream.feed(lines)
            except (serial.SerialException, OSError):
                self.stream.status = f"{self.port}: disconnected - retrying"
            finally:
                ser.close()

    def stop(self):
        self.stop_evt.set()


class SimSource(threading.Thread):
    """Synthetic signal fed through the same text parser as real serial data."""

    def __init__(self, kind, fs, stream):
        super().__init__(daemon=True)
        self.kind, self.fs, self.stream = kind, fs, stream
        self.stop_evt = threading.Event()

    def run(self):
        self.stream.status = f"SIM ({self.kind}, {self.fs:g} Hz)"
        rng = np.random.default_rng(0)
        n0, t0 = 0, time.monotonic()
        while not self.stop_evt.wait(0.02):
            due = int((time.monotonic() - t0) * self.fs)
            if due <= n0:
                continue
            t = np.arange(n0, due) / self.fs
            n0 = due
            rows, names = self._signal(t, rng)
            self.stream.feed([",".join(f"{nm}:{v:.5f}" for nm, v in zip(names, r)) for r in rows])

    def _signal(self, t, rng):
        noise = lambda s: s * rng.standard_normal(t.size)
        if self.kind == "tremor":
            # Every 16 s: a 4 s drink (slow ~0.8 Hz tilt) then 6 s of 8 Hz tremor,
            # mimicking the XG24 output (accel in g, gyro in deg/s with a small bias).
            phase = t % 16
            ramp = lambda a, b: np.clip(np.minimum(phase - a, b - phase), 0, 1)
            drink, trem = ramp(2, 6), ramp(9, 15)
            slow = np.sin(2 * np.pi * 0.8 * t)
            w = 2 * np.pi * 8.0 * t
            ax = 0.25 * drink * slow + 0.10 * trem * np.sin(w) + noise(0.003)
            ay = 0.08 * trem * np.sin(w + 1) + noise(0.003)
            az = 1 - 0.1 * drink * (1 - np.cos(2 * np.pi * 0.8 * t)) + 0.05 * trem * np.sin(w + 2) + noise(0.003)
            gx = 45 * drink * slow + 25 * trem * np.sin(w) + noise(1.0) + 0.5
            gy = 10 * drink * slow + 15 * trem * np.sin(w + 1) + noise(1.0) - 3.4
            gz = 12 * trem * np.sin(w + 2) + noise(1.0)
            return (np.column_stack([ax, ay, az, gx, gy, gz]),
                    ["ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps"])
        # vpu: 2 s voiced bursts (140 Hz + harmonics) alternating with chewing thumps.
        voiced = (t % 4) < 2
        voice = sum(0.3 / k * np.sin(2 * np.pi * 140 * k * t) for k in range(1, 5))
        tc = t % 0.8
        chew = 0.4 * np.exp(-tc / 0.04) * np.sin(2 * np.pi * 25 * tc) * ~voiced
        return np.column_stack([voice * voiced + chew + noise(0.02)]), ["vpu"]

    def stop(self):
        self.stop_evt.set()


# --------------------------------------------------------------------------- spectra

def welch_psd(x, fs, seg_n):
    """Hann-windowed, 50 %-overlap Welch PSD (one-sided, units^2/Hz)."""
    seg_n = int(max(16, min(seg_n, len(x))))
    nfft = 1 << int(np.ceil(np.log2(seg_n)) + 1)  # 2x zero-pad for a smoother curve
    segs = np.lib.stride_tricks.sliding_window_view(x, seg_n)[:: max(1, seg_n // 2)]
    segs = segs - segs.mean(axis=1, keepdims=True)
    win = np.hanning(seg_n)
    spec = np.abs(np.fft.rfft(segs * win, n=nfft)) ** 2 / (fs * (win ** 2).sum())
    spec[:, 1:-1] *= 2
    return np.fft.rfftfreq(nfft, 1 / fs), spec.mean(axis=0)


def multi_psd(X, fs, seg_n):
    """Sum of per-axis Welch PSDs (orientation-independent; X is samples x axes)."""
    freqs, total = None, 0
    for k in range(X.shape[1]):
        freqs, p = welch_psd(X[:, k], fs, seg_n)
        total = total + p
    return freqs, total


def refine_peak(freqs, psd, i):
    """Parabolic interpolation on log power around bin i."""
    if 0 < i < len(psd) - 1:
        a, b, c = np.log(psd[i - 1:i + 2] + 1e-30)
        denom = a - 2 * b + c
        if denom < 0:
            return freqs[i] + 0.5 * (a - c) / denom * (freqs[1] - freqs[0])
    return freqs[i]


def channel_groups(labels):
    """Multi-axis groups by label: {'accel': [...], 'gyro': [...]} (or first 3 channels)."""
    low = [lab.lower() for lab in labels]
    groups = {
        "accel": [i for i, n in enumerate(low) if n.startswith("a") or "acc" in n],
        "gyro": [i for i, n in enumerate(low) if n.startswith("g") or "gyr" in n],
    }
    groups = {k: v for k, v in groups.items() if len(v) >= 2}
    if not groups and len(labels) >= 2:
        groups = {"channels": list(range(min(3, len(labels))))}
    return groups


# --------------------------------------------------------------------------- UI

def spin(lo, hi, val, step, decimals=1, suffix=""):
    s = QtWidgets.QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(val)
    s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    return s


class Dashboard(QtWidgets.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.p = PRESETS[args.preset]
        self.stream = Stream()
        self.source = None
        self.paused = False
        self.rate_hist = collections.deque()
        self.fs = None
        self.labels = None
        self.curves = []
        self.spec_img = None
        self.spec_fmax = None
        self.detect_votes = 0
        self.detected = False
        self.rec_file = None

        self.setWindowTitle(f"{args.title or args.preset.upper()} - live dashboard")
        self.resize(1500, 950)
        self._build_ui()
        self._start_source(args.port)

        self.plot_timer = QtCore.QTimer(timeout=self._update_plot)
        self.plot_timer.start(PLOT_MS)
        self.ana_timer = QtCore.QTimer(timeout=self._update_analysis)
        self.ana_timer.start(ANALYSIS_MS)

    # ---- layout
    def _build_ui(self):
        p = self.p
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)

        bar = QtWidgets.QHBoxLayout()
        root.addLayout(bar)
        self.port_box = QtWidgets.QComboBox()
        self._fill_ports()
        self.port_box.activated.connect(lambda _: self._start_source(self.port_box.currentData()))
        refresh = QtWidgets.QPushButton("↻")
        refresh.setFixedWidth(32)
        refresh.clicked.connect(self._fill_ports)
        self.chan_box = QtWidgets.QComboBox()
        self.chan_box.setMinimumWidth(150)
        self.band_lo = spin(0, 20000, p["band"][0], 0.5, suffix=" Hz")
        self.band_hi = spin(0, 20000, p["band"][1], 0.5, suffix=" Hz")
        self.fmin = spin(0, 20000, p["fmin"], 0.5, suffix=" Hz")
        self.fmax = spin(0, 20000, p["fmax"], 1, suffix=" Hz")
        self.fmax.setSpecialValueText("Nyquist")
        self.ratio = spin(0.05, 1.0, p["ratio"], 0.05, decimals=2)
        self.min_amp = spin(0, 1e6, p["min_amp"], 1, decimals=1)
        self.center = QtWidgets.QCheckBox("Center")
        self.center.setChecked(True)
        self.show_all = QtWidgets.QCheckBox("All channels")
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._set_paused)
        self.rec_btn = QtWidgets.QPushButton("● Record")
        self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self._set_recording)
        for label, w in [("Port", self.port_box), (None, refresh), ("Analyse", self.chan_box),
                         ("Band", self.band_lo), ("–", self.band_hi), ("Ignore <", self.fmin),
                         ("Max", self.fmax), ("Detect ratio ≥", self.ratio), ("Min amp", self.min_amp),
                         (None, self.center),
                         (None, self.show_all),
                         (None, self.pause_btn), (None, self.rec_btn)]:
            if label:
                bar.addWidget(QtWidgets.QLabel(label))
            bar.addWidget(w)
        bar.addStretch()

        grid = QtWidgets.QGridLayout()
        root.addLayout(grid, 1)

        self.time_plot = pg.PlotWidget(title="Signal")
        self.time_plot.setLabel("bottom", "time", "s")
        self.time_plot.showGrid(x=True, y=True, alpha=0.2)
        self.time_plot.addLegend(offset=(10, 5))
        grid.addWidget(self.time_plot, 0, 0, 1, 2)

        self.psd_plot = pg.PlotWidget(title="Power spectral density")
        self.psd_plot.setLogMode(y=True)
        self.psd_plot.setLabel("bottom", "frequency", "Hz")
        self.psd_plot.setLabel("left", "power / Hz")
        self.psd_plot.getAxis("left").enableAutoSIPrefix(False)
        self.psd_plot.showGrid(x=True, y=True, alpha=0.2)
        self.band_region = pg.LinearRegionItem(values=p["band"], brush=(255, 183, 77, 40),
                                               pen=pg.mkPen("#ffb74d"))
        self.psd_plot.addItem(self.band_region)
        self.psd_curve = self.psd_plot.plot(pen=pg.mkPen("#4fc3f7", width=2))
        self.peak_line = pg.InfiniteLine(angle=90, pen=pg.mkPen("#ef5350", width=2, style=QtCore.Qt.PenStyle.DashLine))
        self.psd_plot.addItem(self.peak_line)
        grid.addWidget(self.psd_plot, 1, 0)

        grid.addWidget(self._build_readout(), 1, 1)

        self.spec_plot = pg.PlotWidget(title="Spectrogram")
        self.spec_plot.setLabel("bottom", "seconds ago")
        self.spec_plot.setLabel("left", "frequency", "Hz")
        self.spec_item = pg.ImageItem()
        self.spec_item.setColorMap(pg.colormap.get("inferno"))
        self.spec_plot.addItem(self.spec_item)
        dash = pg.mkPen("#ffffff", width=1, style=QtCore.Qt.PenStyle.DashLine)
        self.spec_lo = pg.InfiniteLine(angle=0, pen=dash)
        self.spec_hi = pg.InfiniteLine(angle=0, pen=dash)
        self.spec_plot.addItem(self.spec_lo)
        self.spec_plot.addItem(self.spec_hi)
        grid.addWidget(self.spec_plot, 2, 0, 1, 2)

        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 3)
        grid.setRowStretch(1, 4)
        grid.setRowStretch(2, 3)

        # Keep the draggable band region and the spinboxes in sync.
        self.band_region.sigRegionChanged.connect(self._region_to_spin)
        self.band_lo.valueChanged.connect(self._spin_to_region)
        self.band_hi.valueChanged.connect(self._spin_to_region)
        self._spin_to_region()

        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)

        QtGui.QShortcut(QtGui.QKeySequence("Space"), self, lambda: self.pause_btn.toggle())
        QtGui.QShortcut(QtGui.QKeySequence("R"), self, lambda: self.rec_btn.toggle())

    def _build_readout(self):
        box = QtWidgets.QFrame()
        box.setStyleSheet("QFrame{background:#161b22;border-radius:8px} QLabel{color:#d0d4d8}")
        lay = QtWidgets.QVBoxLayout(box)
        cap = lambda text: QtWidgets.QLabel(text, styleSheet="font-size:14px;color:#8b949e")
        self.peak_lbl = QtWidgets.QLabel("--", styleSheet="font-size:56px;font-weight:bold;color:#4fc3f7")
        self.bpm_lbl = QtWidgets.QLabel("", styleSheet="font-size:16px")
        self.ratio_lbl = QtWidgets.QLabel("--", styleSheet="font-size:28px;font-weight:bold")
        self.rms_lbl = QtWidgets.QLabel("--", styleSheet="font-size:18px")
        self.detect_lbl = QtWidgets.QLabel("", alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        self._set_detect(False)
        for w in [cap("Peak frequency"), self.peak_lbl, self.bpm_lbl, cap("Power in band"),
                  self.ratio_lbl, cap("Amplitude in band (RMS)"), self.rms_lbl]:
            lay.addWidget(w)
        lay.addStretch()
        lay.addWidget(self.detect_lbl)
        return box

    def _fill_ports(self):
        current = self.port_box.currentData()
        self.port_box.clear()
        self.port_box.addItem("SIM (synthetic)", SIM)
        for info in serial.tools.list_ports.comports():
            self.port_box.addItem(f"{info.device} - {info.description}", info.device)
        idx = self.port_box.findData(current or self.args.port)
        if idx < 0 and current is None and self.args.port:
            self.port_box.addItem(self.args.port, self.args.port)
            idx = self.port_box.count() - 1
        self.port_box.setCurrentIndex(max(idx, 0))

    # ---- source control
    def _start_source(self, port):
        if self.source:
            self.source.stop()
            self.source.join(timeout=1)
        self.stream.reset()
        self.rate_hist.clear()
        self.fs = None
        self.labels = None
        self.spec_img = None
        if port == SIM:
            self.source = SimSource(self.args.preset, self.args.fs or self.p["sim_fs"], self.stream)
        else:
            self.source = SerialSource(port, self.args.baud, self.stream)
        self.source.start()

    def _set_paused(self, on):
        self.paused = on
        self.pause_btn.setText("Resume" if on else "Pause")

    def _set_recording(self, on):
        if on:
            labels = self.stream.labels or []
            out = Path("recordings")
            out.mkdir(exist_ok=True)
            path = out / f"{self.args.preset}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            self.rec_file = open(path, "w", newline="")
            writer = csv.writer(self.rec_file)
            writer.writerow(["host_time", *labels])
            self.stream.recorder = writer
            self.rec_btn.setText(f"■ {path.name}")
            self.rec_btn.setStyleSheet("background:#b71c1c;color:white")
        else:
            with self.stream.lock:
                self.stream.recorder = None
            if self.rec_file:
                self.rec_file.close()
                self.rec_file = None
            self.rec_btn.setText("● Record")
            self.rec_btn.setStyleSheet("")

    def _region_to_spin(self):
        lo, hi = self.band_region.getRegion()
        for s, v in ((self.band_lo, lo), (self.band_hi, hi)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
        self.spec_lo.setPos(lo)
        self.spec_hi.setPos(hi)

    def _spin_to_region(self):
        lo, hi = sorted((self.band_lo.value(), self.band_hi.value()))
        self.band_region.blockSignals(True)
        self.band_region.setRegion((lo, hi))
        self.band_region.blockSignals(False)
        self.spec_lo.setPos(lo)
        self.spec_hi.setPos(hi)

    # ---- data helpers
    def _estimate_fs(self, data):
        if self.args.fs:
            return self.args.fs
        if self.args.time_col is not None and data is not None and len(data) > 10:
            d = np.diff(data[-2000:, self.args.time_col])
            d = d[d > 0]
            if d.size:
                return 1.0 / (np.median(d) * {"us": 1e-6, "ms": 1e-3, "s": 1.0}[self.args.time_unit])
        now, total = time.monotonic(), self.stream.total
        if self.rate_hist and total < self.rate_hist[-1][1]:
            self.rate_hist.clear()
        self.rate_hist.append((now, total))
        while self.rate_hist and now - self.rate_hist[0][0] > 4:
            self.rate_hist.popleft()
        t0, n0 = self.rate_hist[0]
        if now - t0 < 1.5 or total == n0:
            return self.fs
        est = (total - n0) / (now - t0)
        return est if self.fs is None else 0.95 * self.fs + 0.05 * est

    def _channels(self, data, labels):
        """Drop the timestamp column, if any."""
        tc = self.args.time_col
        if tc is None or data.shape[1] <= 1:
            return data, labels
        keep = [i for i in range(data.shape[1]) if i != tc]
        return data[:, keep], [labels[i] for i in keep]

    def _refresh_channel_box(self, labels):
        if labels == self.labels:
            return
        self.labels = labels
        self.chan_box.clear()
        for i, lab in enumerate(labels):
            self.chan_box.addItem(lab, [i])
        groups = channel_groups(labels)
        for name, cols in groups.items():
            self.chan_box.addItem(f"{name} (all {len(cols)} axes)", cols)
        if self.args.preset == "tremor" and groups:
            # Tremor rotates the hand, so the gyro is the most direct signal.
            name = "gyro" if "gyro" in groups else next(iter(groups))
            self.chan_box.setCurrentIndex(len(labels) + list(groups).index(name))
        self.time_plot.clear()
        self.time_plot.plotItem.legend.clear()
        self.curves = []
        for i, lab in enumerate(labels):
            c = self.time_plot.plot(pen=pg.mkPen(COLORS[i % len(COLORS)], width=1.5), name=lab)
            c.setDownsampling(auto=True, method="peak")
            c.setClipToView(True)
            self.curves.append(c)

    def _analysis_signal(self, data):
        """Selected column(s) as a samples x axes array."""
        sel = self.chan_box.currentData()
        if not sel or max(sel) >= data.shape[1]:
            return None
        return data[:, sel]

    def _amp_unit(self):
        sel = self.chan_box.currentData() or []
        names = [self.labels[i].lower() for i in sel if self.labels and i < len(self.labels)]
        if names and all(n.endswith("dps") for n in names):
            return " °/s"
        if names and all(n.endswith("_g") for n in names):
            return " g"
        return ""

    # ---- updates
    def _update_plot(self):
        snap_n = int((self.fs or 1000) * max(self.p["window"], self.p["psd_seconds"])) + 10
        data, total, labels = self.stream.snapshot(max(snap_n, 2000))
        self.fs = self._estimate_fs(data)
        s = self.stream
        fs_txt = f"{self.fs:,.1f} Hz" if self.fs else "measuring..."
        msg = f"{s.status}   |   fs {fs_txt}   |   samples {s.good:,}   |   bytes {s.bytes:,}"
        if s.bad:
            msg += f"   |   skipped {s.bad:,} (last: {s.last_bad!r})"
        if s.bytes > 2000 and s.good == 0:
            msg += "   |   NO NUMERIC LINES - check baud rate / print format"
        self.status.setText(msg)
        if data is None or self.paused or not self.fs:
            return
        data, labels = self._channels(data, labels)
        self._refresh_channel_box(labels)
        m = min(len(data), int(self.fs * self.p["window"]))
        view = data[-m:]
        if self.center.isChecked():
            view = view - view.mean(axis=0)
        t = (np.arange(m) - m) / self.fs
        # Show only the analysed channels unless "All channels" is ticked (gyro dwarfs accel).
        shown = self.chan_box.currentData() or []
        for i, c in enumerate(self.curves):
            c.setVisible(self.show_all.isChecked() or i in shown)
            c.setData(t, view[:, i])

    def _update_analysis(self):
        if self.paused or not self.fs:
            return
        fs = self.fs
        data, _, labels = self.stream.snapshot(int(fs * self.p["psd_seconds"]))
        if data is None or len(data) < max(32, fs * 0.5):
            return
        data, _ = self._channels(data, labels)
        x = self._analysis_signal(data)
        if x is None:
            return
        x = x - x.mean(axis=0)
        seg_n = int(fs * self.p["seg"])
        freqs, psd = multi_psd(x, fs, seg_n)
        nyq = float(np.round(fs / 2, -1 if fs > 200 else 0))  # stable despite fs jitter
        fmax = min(self.fmax.value() or nyq, nyq)
        fmin = self.fmin.value()
        lo, hi = sorted((self.band_lo.value(), self.band_hi.value()))

        show = freqs <= fmax
        self.psd_curve.setData(freqs[show][1:], psd[show][1:] + 1e-20)
        self.psd_plot.setXRange(0, fmax, padding=0)

        valid = (freqs >= fmin) & (freqs <= fmax)
        if not valid.any():
            return
        idx = np.flatnonzero(valid)
        i = idx[np.argmax(psd[idx])]
        peak = refine_peak(freqs, psd, i)
        band = (freqs >= lo) & (freqs <= hi) & valid
        ratio = psd[band].sum() / max(psd[valid].sum(), 1e-30)
        amp = float(np.sqrt(psd[band].sum() * (freqs[1] - freqs[0])))  # RMS of the in-band part

        self.peak_line.setPos(peak)
        self.peak_lbl.setText(f"{peak:.2f} Hz" if peak < 100 else f"{peak:.0f} Hz")
        self.bpm_lbl.setText(f"{peak * 60:.0f} cycles/min" if peak < 5 else "")
        self.ratio_lbl.setText(f"{ratio * 100:.0f} %  of {fmin:g}–{fmax:.0f} Hz")
        self.rms_lbl.setText(f"{amp:.3g}{self._amp_unit()}")

        # Tremor = most oscillation power inside the band AND enough of it to matter.
        hit = ratio >= self.ratio.value() and amp >= self.min_amp.value()
        # Debounce: switch on after 3 consistent updates (0.3 s), off after 8 (0.8 s) so
        # the badge doesn't flicker when the tremor briefly changes rhythm.
        self.detect_votes = self.detect_votes + 1 if hit != self.detected else 0
        if self.detect_votes >= (8 if self.detected else 3):
            self.detected, self.detect_votes = hit, 0
            self._set_detect(hit)

        self._update_spectrogram(x, fs, fmax)

    def _update_spectrogram(self, x, fs, fmax):
        seg_n = int(min(len(x), max(16, fs * self.p["seg"])))
        freqs, col = multi_psd(x[-seg_n:], fs, seg_n)
        grid = np.linspace(0, fmax, SPEC_ROWS)
        col_db = np.interp(grid, freqs, 10 * np.log10(col + 1e-20))
        span = SPEC_COLS * ANALYSIS_MS / 1000
        reset = self.spec_img is None or self.spec_fmax != fmax
        if reset:
            self.spec_img = np.full((SPEC_ROWS, SPEC_COLS), np.nan)
            self.spec_fmax = fmax
            self.spec_top_db = None
            self.spec_plot.setXRange(-span, 0, padding=0)
            self.spec_plot.setYRange(0, fmax, padding=0)
        self.spec_img = np.roll(self.spec_img, -1, axis=1)
        self.spec_img[:, -1] = col_db
        filled = self.spec_img[np.isfinite(self.spec_img)]
        # Brightness ceiling holds the loudest event and decays ~0.5 dB/s, so after a
        # real tremor the resting noise floor goes dark instead of being auto-stretched.
        top = np.percentile(filled, 99.7)
        if self.spec_top_db is not None:
            top = max(top, self.spec_top_db - 0.05)
        self.spec_top_db = top
        lo_db = max(np.percentile(filled, 20), top - 40)
        img = np.nan_to_num(self.spec_img, nan=lo_db)
        self.spec_item.setImage(img, levels=(lo_db, max(top, lo_db + 1)), autoLevels=False)
        if reset:  # rect must be set after the image exists
            self.spec_item.setRect(QtCore.QRectF(-span, 0, span, fmax))

    def _set_detect(self, on):
        label = self.args.detect_label or self.p["label"]
        if on:
            self.detect_lbl.setText(f"{label} DETECTED")
            self.detect_lbl.setStyleSheet("font-size:30px;font-weight:bold;padding:18px;"
                                          "background:#c62828;color:white;border-radius:8px")
        else:
            self.detect_lbl.setText(f"no {label.lower()}")
            self.detect_lbl.setStyleSheet("font-size:24px;padding:18px;background:#1f2a33;"
                                          "color:#8b949e;border-radius:8px")

    def closeEvent(self, ev):
        if self.source:
            self.source.stop()
        self._set_recording(False)
        super().closeEvent(ev)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", choices=PRESETS, default="tremor")
    ap.add_argument("--port", default=None, help="COM port, or SIM for synthetic data")
    ap.add_argument("--baud", type=int, default=115200, help="must match Serial.begin()")
    ap.add_argument("--fs", type=float, default=None,
                    help="known sample rate in Hz (otherwise measured from arrival rate)")
    ap.add_argument("--time-col", type=int, default=None,
                    help="column index holding a timestamp (used for fs, not plotted)")
    ap.add_argument("--time-unit", choices=["us", "ms", "s"], default="us")
    ap.add_argument("--title", default=None)
    ap.add_argument("--detect-label", default=None, help="text for the detection badge")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    ap.add_argument("--snapshot", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--snapshot-after", type=float, default=15, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.list:
        for info in serial.tools.list_ports.comports():
            print(f"{info.device:8s} {info.description}")
        return
    if args.port is None:
        ports = serial.tools.list_ports.comports()
        args.port = ports[0].device if len(ports) == 1 else SIM

    pg.setConfigOptions(imageAxisOrder="row-major", background="#0d1117", foreground="#d0d4d8")
    app = pg.mkQApp("Live dashboard")
    win = Dashboard(args)
    win.show()
    if args.snapshot:
        def snap():
            win.grab().save(args.snapshot)
            app.quit()
        QtCore.QTimer.singleShot(int(args.snapshot_after * 1000), snap)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
