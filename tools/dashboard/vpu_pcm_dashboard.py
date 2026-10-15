"""Live view and trial recorder for the VPU PCM stream (firmware/vpu_pcm_stream):
waveform, spectrogram, band envelope with live event detection, and per-trial analysis.

    python tools/dashboard/vpu_pcm_dashboard.py --port COM3
    python tools/dashboard/vpu_pcm_dashboard.py --port SIM      # synthetic footsteps

The board is switched to binary mode while the window is open and back to stats mode when
it closes. The noise baseline is learned from the first 3 s after connecting (keep still)
or with the "Learn noise" button; an event is when the 50 ms RMS in the chosen band rises
the threshold above it.

Trial tab: fill in the trial (type, distance, impacts made, floor, ...) and press Record;
Stop saves the same CSV, JSON sidecar and .mat files as `python -m acquisition.record`, to
data/raw/<date>/ by default, then runs everyday_sensing.impacts on it. Results tab: the
impacts found in the last trial and a per-distance summary of every trial in that folder,
each measured against the latest noise-only trial (record one first).

Keys: Space = pause display, R = start/stop recording, N = learn noise, C = clear events.
Requires the repo's Python package (pip install -r requirements.txt from the repo root).
"""

import argparse
import dataclasses
import datetime
import math
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import serial
import serial.tools.list_ports
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets
from scipy import signal

from acquisition.frames import Frame, FrameParser
from acquisition.record import (
    DEFAULT_OUT_DIR,
    NOMINAL_FS_HZ,
    assemble,
    build_meta,
    next_trial_id,
    parse_args,
    repo_state,
    write_trial,
)
from everyday_sensing import impacts
from everyday_sensing.events import BandEnvelope, EventDetector

SIM = "SIM"
TICK_MS = 50
WAVE_S = 3.0           # waveform history shown
ENV_S = 20.0           # envelope history shown
SPEC_S = 10.0          # spectrogram history shown
SPEC_N = 1024          # samples per spectrogram column
SPEC_FMAX = 8000.0     # top of the sensor's specified band (datasheet)
LEARN_S = 3.0
BANDS = {
    "100-500 Hz (analysis band)": (100.0, 500.0),
    "above 100 Hz": (100.0, None),
    "100-2000 Hz": (100.0, 2000.0),
    "2-8 kHz (resonance)": (2000.0, 8000.0),
}
COLORS = {"wave": "#4fc3f7", "env": "#ffb74d", "thr": "#ef5350", "event": "#66bb6a"}
TRIAL_TYPES = ["footsteps", "stomp", "drop", "tap", "noise only"]



# --------------------------------------------------------------------------- data

class PcmRing:
    """Thread-safe ring of int16 samples with a running total for cursors."""

    def __init__(self, seconds, fs):
        self.cap = int(seconds * fs)
        self.buf = np.zeros(self.cap, dtype=np.int16)
        self.total = 0
        self.lock = threading.Lock()

    def write(self, x):
        x = x[-self.cap:]
        with self.lock:
            i = self.total % self.cap
            k = min(x.size, self.cap - i)
            self.buf[i:i + k] = x[:k]
            self.buf[:x.size - k] = x[k:]
            self.total += x.size

    def since(self, cursor):
        """Samples written after `cursor`, the new cursor, and how many were overwritten."""
        with self.lock:
            lost = max(0, self.total - cursor - self.cap)
            start = max(cursor, self.total - self.cap)
            idx = np.arange(start, self.total) % self.cap
            return self.buf[idx].copy(), self.total, lost

    def latest(self, n):
        with self.lock:
            n = min(n, self.total, self.cap)
            idx = np.arange(self.total - n, self.total) % self.cap
            return self.buf[idx].copy()


class Link:
    """State shared by a source thread and the window."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"
        self.parser = FrameParser()
        self.frames = 0
        self.gaps = self.missing = 0
        self.ovf_first = self.ovf_last = None
        self.expected = None
        self.recording = None   # list of Frames while recording

    def on_frames(self, frames, ring):
        with self.lock:
            self.frames += len(frames)
            for f in frames:
                if self.expected is not None and f.first_sample > self.expected:
                    self.gaps += 1
                    self.missing += f.first_sample - self.expected
                self.expected = f.first_sample + f.samples.size
                self.ovf_first = f.ovf if self.ovf_first is None else self.ovf_first
                self.ovf_last = f.ovf
                ring.write(f.samples)
                if self.recording is not None:
                    self.recording.append(f)


class SerialPcmSource(threading.Thread):
    def __init__(self, port, link, ring):
        super().__init__(daemon=True)
        self.port, self.link, self.ring = port, link, ring
        self.stop_evt = threading.Event()

    def run(self):
        while not self.stop_evt.is_set():
            try:
                ser = serial.Serial()
                ser.port, ser.baudrate, ser.timeout = self.port, 115200, 0.05
                ser.dtr, ser.rts = True, False  # RTS high would hold the ESP32-C6 in reset
                ser.open()
            except (serial.SerialException, OSError) as e:
                hint = " (close Arduino Serial Monitor?)" if "denied" in str(e).lower() else ""
                self.link.status = f"{self.port}: cannot open{hint} - retrying"
                self.stop_evt.wait(1.0)
                continue
            self.link.status = f"{self.port} streaming"
            try:
                ser.reset_input_buffer()
                ser.write(b"B")
                t0 = time.monotonic()
                while not self.stop_evt.is_set():
                    data = ser.read(65536)
                    if data:
                        frames = self.link.parser.feed(data, time.monotonic() - t0)
                        self.link.on_frames(frames, self.ring)
                ser.write(b"S")
            except (serial.SerialException, OSError):
                self.link.status = f"{self.port}: disconnected - retrying"
            finally:
                ser.close()

    def stop(self):
        self.stop_evt.set()


class SimPcmSource(threading.Thread):
    """Synthetic stream in 128-sample frames: noise, a 41 Hz hum like the one measured at
    rest, and bursts of footsteps that ring at the sensor's resonance."""

    def __init__(self, link, ring, fs=NOMINAL_FS_HZ):
        super().__init__(daemon=True)
        self.link, self.ring, self.fs = link, ring, fs
        self.stop_evt = threading.Event()

    def run(self):
        self.link.status = "SIM (synthetic footsteps)"
        rng = np.random.default_rng(2)
        n, t0, ovf = 0, time.monotonic(), 0
        while not self.stop_evt.wait(0.01):
            due = int((time.monotonic() - t0) * self.fs) // 128 * 128
            frames = []
            while n < due:
                t = (n + np.arange(128)) / self.fs
                x = 3 * rng.standard_normal(128) + 30 * np.sin(2 * np.pi * 41 * t)
                c = t % 8.0                     # 6 steps 0.55 s apart, then 4.7 s quiet
                for k in range(6):
                    dt = c - (1.0 + 0.55 * k)
                    a = np.where(dt >= 0, 1.0, 0.0) * (0.6 + 0.4 * (k % 3) / 2)
                    x += a * (800 * np.exp(-dt / 0.012) * np.sin(2 * np.pi * 4500 * dt)
                              + 60 * np.exp(-dt / 0.04) * np.sin(2 * np.pi * 300 * dt))
                s = np.clip(np.round(x), -32768, 32767).astype(np.int16)
                frames.append(Frame(n, s, ovf, 0, time.monotonic() - t0))
                n += 128
            self.link.on_frames(frames, self.ring)

    def stop(self):
        self.stop_evt.set()


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


class PcmDashboard(QtWidgets.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.fs = args.fs_hz
        self.source = None
        self.paused = False
        self.rec_start = None
        self.events = []
        self.analysis_cache = {}
        self.hp_sos = signal.butter(4, 100, "highpass", fs=self.fs, output="sos")
        self.setWindowTitle("VPU PCM - live")
        self.resize(1500, 950)
        self._build_ui()
        self._refresh_results()
        self._start_source(args.port)
        self.timer = QtCore.QTimer(timeout=self._tick)
        self.timer.start(TICK_MS)

    # ---- layout
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        bar = QtWidgets.QHBoxLayout()
        root.addLayout(bar)

        self.port_box = QtWidgets.QComboBox()
        self._fill_ports()
        self.port_box.activated.connect(lambda _: self._start_source(self.port_box.currentData()))
        self.band_box = QtWidgets.QComboBox()
        for name in BANDS:
            self.band_box.addItem(name)
        self.band_box.currentIndexChanged.connect(lambda _: self._reset_detection())
        self.thr = spin(3, 60, impacts.THRESHOLD_DB, 1, decimals=0, suffix=" dB")
        self.gap = spin(0.05, 5, 0.3, 0.05, decimals=2, suffix=" s")
        self.hp = QtWidgets.QCheckBox("High-pass 100 Hz", checked=True)
        learn = QtWidgets.QPushButton("Learn noise")
        learn.clicked.connect(self._learn)
        clear = QtWidgets.QPushButton("Clear events")
        clear.clicked.connect(self._clear)
        self.pause_btn = QtWidgets.QPushButton("Pause", checkable=True)
        self.pause_btn.toggled.connect(self._set_paused)
        for label, w in [("Port", self.port_box), ("Band", self.band_box),
                         ("Threshold", self.thr), ("Min gap", self.gap), (None, self.hp),
                         (None, learn), (None, clear), (None, self.pause_btn)]:
            if label:
                bar.addWidget(QtWidgets.QLabel(label))
            bar.addWidget(w)
        bar.addStretch()

        grid = QtWidgets.QGridLayout()
        root.addLayout(grid, 1)
        self.wave = pg.PlotWidget(title="Waveform (counts)")
        self.wave.setLabel("bottom", "time", "s")
        self.wave.showGrid(x=True, y=True, alpha=0.2)
        self.wave_curve = self.wave.plot(pen=pg.mkPen(COLORS["wave"], width=1))
        self.wave_curve.setDownsampling(auto=True, method="peak")
        self.wave_curve.setClipToView(True)
        grid.addWidget(self.wave, 0, 0)

        self.env = pg.PlotWidget(title="Band level above noise baseline (50 ms RMS)")
        self.env.setLabel("bottom", "time", "s")
        self.env.setLabel("left", "dB")
        self.env.showGrid(x=True, y=True, alpha=0.2)
        self.env.setXRange(-ENV_S, 0, padding=0)
        self.env_curve = self.env.plot(pen=pg.mkPen(COLORS["env"], width=1.5))
        self.thr_line = pg.InfiniteLine(
            angle=0, movable=True, pen=pg.mkPen(COLORS["thr"], width=2, style=QtCore.Qt.PenStyle.DashLine),
            label="threshold {value:.0f} dB", labelOpts=dict(position=0.1, color=COLORS["thr"]))
        self.thr_line.setValue(self.thr.value())
        self.thr_line.sigPositionChanged.connect(lambda: self.thr.setValue(round(self.thr_line.value())))
        self.thr.valueChanged.connect(self._threshold_changed)
        self.env.addItem(self.thr_line)
        self.event_scatter = pg.ScatterPlotItem(size=9, brush=pg.mkBrush(COLORS["event"]), pen=None)
        self.env.addItem(self.event_scatter)
        grid.addWidget(self.env, 1, 0)

        self.spec = pg.PlotWidget(title="Spectrogram (dB)")
        self.spec.setLabel("bottom", "time", "s")
        self.spec.setLabel("left", "frequency", "Hz")
        self.spec_img = pg.ImageItem()
        self.spec_img.setColorMap(pg.colormap.get("viridis"))
        self.spec.addItem(self.spec_img)
        self.spec_cols = int(SPEC_S * self.fs / SPEC_N)
        self.spec_bins = int(SPEC_FMAX / (self.fs / SPEC_N))
        self.spec_data = np.full((self.spec_cols, self.spec_bins), np.nan)
        self.spec_rect = QtCore.QRectF(-SPEC_S, 0, SPEC_S, self.spec_bins * self.fs / SPEC_N)
        self.spec.setXRange(-SPEC_S, 0, padding=0)
        self.spec.setYRange(0, SPEC_FMAX, padding=0)
        grid.addWidget(self.spec, 2, 0)

        grid.addWidget(self._build_panel(), 0, 1, 3, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)

        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)
        QtGui.QShortcut(QtGui.QKeySequence("Space"), self, lambda: self.pause_btn.toggle())
        QtGui.QShortcut(QtGui.QKeySequence("R"), self, lambda: self.rec_btn.toggle())
        QtGui.QShortcut(QtGui.QKeySequence("N"), self, self._learn)
        QtGui.QShortcut(QtGui.QKeySequence("C"), self, self._clear)

    def _build_panel(self):
        tabs = QtWidgets.QTabWidget()
        tabs.setMinimumWidth(420)
        tabs.setStyleSheet("QWidget#tab{background:#161b22} QLabel{color:#d0d4d8}")
        cap = lambda text: QtWidgets.QLabel(text, styleSheet="font-size:14px;color:#8b949e")
        center = QtCore.Qt.AlignmentFlag.AlignCenter

        def page(widgets):
            w = QtWidgets.QWidget(objectName="tab")
            lay = QtWidgets.QVBoxLayout(w)
            for x in widgets:
                if isinstance(x, QtWidgets.QLayout):
                    lay.addLayout(x)
                else:
                    lay.addWidget(x)
            return w

        # Trial: what is being recorded
        self.type_box = QtWidgets.QComboBox()
        self.type_box.addItems(TRIAL_TYPES)
        self.dist = spin(0, 100, 1.0, 0.5, decimals=2, suffix=" m")
        self.made = QtWidgets.QSpinBox(minimum=0, maximum=999, value=0, specialValueText="not given")
        self.floor_edit = QtWidgets.QLineEdit(placeholderText="e.g. tile")
        self.mount_edit = QtWidgets.QLineEdit("tape")
        self.operator_edit = QtWidgets.QLineEdit()
        fw = repo_state().get("fw_source_commit") or ""
        self.fw_edit = QtWidgets.QLineEdit(fw[:7], placeholderText="commit flashed on the board")
        self.prefix_edit = QtWidgets.QLineEdit("fs")
        self.notes_edit = QtWidgets.QLineEdit(placeholderText="e.g. walking circle, 5 steps on marks")
        self.trial_edit = QtWidgets.QLineEdit()
        form = QtWidgets.QFormLayout()
        for label, w in [("Type", self.type_box), ("Distance", self.dist),
                         ("Impacts made", self.made), ("Floor", self.floor_edit),
                         ("Mount", self.mount_edit), ("Operator", self.operator_edit),
                         ("Firmware commit", self.fw_edit), ("ID prefix", self.prefix_edit),
                         ("Notes", self.notes_edit), ("Trial ID", self.trial_edit)]:
            form.addRow(QtWidgets.QLabel(label), w)
        for sig in (self.type_box.currentIndexChanged, self.dist.valueChanged,
                    self.prefix_edit.textChanged):
            sig.connect(lambda *_: self._suggest_trial_id())
        self.type_box.currentIndexChanged.connect(lambda *_: self._type_changed())
        self.rec_btn = QtWidgets.QPushButton("● Record", checkable=True)
        self.rec_btn.setStyleSheet("font-size:18px;padding:10px")
        self.rec_btn.toggled.connect(self._set_recording)
        self.rec_lbl = QtWidgets.QLabel(" ", wordWrap=True, styleSheet="font-size:13px")
        intro = cap("Record a noise-only trial first: it is the baseline for the analysis "
                    "of the trials after it.")
        intro.setWordWrap(True)
        trial = page([intro, form, self.rec_btn,
                      self.rec_lbl, QtWidgets.QWidget()])
        trial.layout().setStretch(trial.layout().count() - 1, 1)

        # Results: last trial and per-distance summary
        self.result_lbl = QtWidgets.QLabel("no trial recorded yet", wordWrap=True,
                                           styleSheet="font-size:15px")
        self.result_table = self._table(["#", "t (s)", "100-500 Hz", "500-2000 Hz", "2-8 kHz"])
        self.summary_table = self._table(["type", "dist.", "n", "found/made",
                                          "100-500", "500-2k", "2-8k"])
        self.folder_lbl = QtWidgets.QLabel(wordWrap=True, styleSheet="font-size:12px;color:#8b949e")
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_results)
        results = page([cap("Last trial (dB above the noise baseline)"), self.result_lbl,
                        self.result_table, cap("All trials in the folder: mean (sd) dB per impact, bands in Hz"),
                        self.summary_table, self.folder_lbl, refresh])

        # Live: events from the band level plot
        self.count_lbl = QtWidgets.QLabel("0", alignment=center,
                                          styleSheet="font-size:64px;font-weight:bold")
        self.last_lbl = QtWidgets.QLabel(" ", alignment=center, styleSheet="font-size:18px")
        self.noise_lbl = QtWidgets.QLabel(styleSheet="font-size:14px", wordWrap=True)
        self.log = QtWidgets.QListWidget()
        self.log.setStyleSheet("QListWidget{background:#0d1117;color:#d0d4d8;font-family:Consolas;"
                               "font-size:13px;border:none}")
        live = page([cap("Live events"), self.count_lbl, self.last_lbl, cap("Live noise baseline"),
                     self.noise_lbl, cap("Event log"), self.log])

        tabs.addTab(trial, "Trial")
        tabs.addTab(results, "Results")
        tabs.addTab(live, "Live")
        self.tabs = tabs
        return tabs

    def _table(self, headers):
        t = QtWidgets.QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        t.setStyleSheet("QTableWidget{background:#0d1117;color:#d0d4d8;gridline-color:#30363d}")
        return t

    def _folder(self) -> Path:
        return Path(self.args.out_dir) / datetime.date.today().isoformat()

    def _type_changed(self):
        noise = self.type_box.currentText() == "noise only"
        for w in (self.dist, self.made):
            w.setEnabled(not noise)

    def _suggest_trial_id(self):
        prefix = self.prefix_edit.text().strip() or "trial"
        self.trial_edit.setText(next_trial_id(self._folder(), prefix, self.type_box.currentText(),
                                              self.dist.value()))

    def _fill_ports(self):
        self.port_box.clear()
        self.port_box.addItem("SIM (synthetic)", SIM)
        for info in serial.tools.list_ports.comports():
            self.port_box.addItem(f"{info.device} - {info.description}", info.device)
        idx = self.port_box.findData(self.args.port)
        if idx < 0:
            self.port_box.addItem(self.args.port, self.args.port)
            idx = self.port_box.count() - 1
        self.port_box.setCurrentIndex(idx)

    # ---- source and detection state
    def _start_source(self, port):
        if self.source:
            self.source.stop()
            self.source.join(timeout=2)
        if self.rec_btn.isChecked():
            self.rec_btn.setChecked(False)
        self.port = port
        self.link = Link()
        self.ring = PcmRing(60, self.fs)
        self.cursor = 0
        self.lost = 0
        self.rate_hist = []
        self.spec_data[:] = np.nan
        self._spec_rem = np.zeros(0)
        self._reset_detection()
        self.source = (SimPcmSource(self.link, self.ring, self.fs) if port == SIM
                       else SerialPcmSource(port, self.link, self.ring))
        self.source.start()

    def _reset_detection(self):
        self.envelope = BandEnvelope(self.fs, BANDS[self.band_box.currentText()])
        self.detector = EventDetector(self.envelope.rate, self.thr.value(), self.thr.value() - 6,
                                      self.gap.value())
        self.env_hist = np.zeros(0)
        self.env_index0 = 0          # envelope index of env_hist[0]
        self.learn_from = 0          # envelope index where noise learning starts
        self.learning = True
        self._clear()

    def _learn(self):
        self.learning = True
        self.learn_from = self.env_index0 + self.env_hist.size

    def _clear(self):
        self.events = []
        self.log.clear()
        self.count_lbl.setText("0")
        self.last_lbl.setText(" ")

    def _threshold_changed(self, v):
        self.thr_line.setValue(v)
        self.detector.threshold_db = v
        self.detector.release_db = v - 6

    def _set_paused(self, on):
        self.paused = on
        self.pause_btn.setText("Resume" if on else "Pause")

    def _set_recording(self, on):
        link = self.link
        if on:
            if not self.trial_edit.text().strip():
                self._suggest_trial_id()
            with link.lock:
                link.recording = []
                self.rec_counts = (link.parser.crc_errors, link.parser.skipped_bytes)
            self.rec_start = time.time()
            self.rec_btn.setText("■ Stop")
            self.rec_btn.setStyleSheet("font-size:18px;padding:10px;background:#b71c1c;color:white")
            self.rec_lbl.setText(f"recording {self.trial_edit.text().strip()}...")
            return
        self.rec_btn.setText("● Record")
        self.rec_btn.setStyleSheet("font-size:18px;padding:10px")
        with link.lock:
            frames, link.recording = link.recording, None
            stats = types.SimpleNamespace(
                crc_errors=link.parser.crc_errors - self.rec_counts[0],
                skipped_bytes=link.parser.skipped_bytes - self.rec_counts[1])
        if not frames:
            self.rec_lbl.setText("nothing recorded")
            return
        t0 = frames[0].host_time_s
        frames = [dataclasses.replace(f, host_time_s=f.host_time_s - t0) for f in frames]
        capture = assemble(frames, stats, self.fs)
        start = datetime.datetime.fromtimestamp(self.rec_start).astimezone()
        trial = self.trial_edit.text().strip()
        try:
            meta = build_meta(parse_args(self._record_argv(trial)), start)
            paths = write_trial(capture, meta, self.args.out_dir)
        except (FileExistsError, SystemExit) as e:
            reason = "trial id already exists" if isinstance(e, FileExistsError) else "invalid form"
            self.rec_lbl.setText(f"not saved ({reason}): {trial}")
            return
        verdict = "checks OK" if capture.checks["ok"] else "CHECKS FAILED (see .json)"
        self.rec_lbl.setText(f"{verdict}, {capture.checks['samples']:,} samples\n"
                             f"saved {trial}.csv/.json/.mat in {paths['json'].parent}")
        self._suggest_trial_id()
        self._refresh_results(show=trial)

    def _record_argv(self, trial):
        argv = ["--port", self.port, "--trial-id", trial,
                "--duration", f"{time.time() - self.rec_start:.3f}", "--fs-hz", str(self.fs)]
        kind = self.type_box.currentText()
        if kind == "noise only":
            argv.append("--noise-only")
        else:
            argv += ["--excitation-type", kind, "--distance-m", f"{self.dist.value():g}"]
            if self.made.value():
                argv += ["--impacts", str(self.made.value())]
        for flag, edit in [("--floor-material", self.floor_edit), ("--mount-method", self.mount_edit),
                           ("--operator", self.operator_edit), ("--fw-commit", self.fw_edit),
                           ("--notes", self.notes_edit)]:
            if edit.text().strip():
                argv += [flag, edit.text().strip()]
        return argv

    def _refresh_results(self, show=None):
        folder = self._folder()
        self._suggest_trial_id()
        if not folder.exists():
            self.folder_lbl.setText(f"{folder} (no trials yet)")
            return
        trials = impacts.analyze_folder(folder, self.thr.value(), self.analysis_cache)
        self.folder_lbl.setText(f"{folder}, {len(trials)} trials, threshold "
                                f"{self.thr.value():.0f} dB")
        rows = impacts.summarize(trials)
        self._fill(self.summary_table, [
            [r["excitation"], f"{r['distance_m']:g} m", r["trials"],
             f"{r['impacts_found']} / {'-' if r['impacts_made'] is None else r['impacts_made']}",
             *[self._mean_sd(r, b) for b in impacts.BANDS]] for r in rows])
        shown = next((t for t in trials if t["meta"]["trial_id"] == show), None)
        if shown is None and show is None:
            shown = trials[-1] if trials else None
        if shown is not None:
            self._show_trial(shown)
            if show is not None:
                self.tabs.setCurrentIndex(1)

    def _show_trial(self, t):
        meta = t["meta"]
        if t["result"] is None:
            self.result_lbl.setText(f"{meta['trial_id']}: noise only. It is the baseline for the "
                                    f"trials recorded after it.")
            self.result_table.setRowCount(0)
            return
        found = t["result"]["impacts"]
        made = meta.get("impacts_made")
        match = "" if made is None else (" (matches)" if made == len(found) else
                                         f" <span style='color:#ffa726'>({made} made)</span>")
        self.result_lbl.setText(
            f"<b>{meta['trial_id']}</b>: {(meta.get('excitation') or {}).get('type')} at "
            f"{meta.get('distance_m'):g} m, <b>{len(found)} found</b>{match}<br>"
            f"baseline: {t['result']['baseline_source']}")
        self._fill(self.result_table, [
            [k, f"{i['t_s']:.2f}", *[f"{i['levels_db'][b]:.1f}" for b in impacts.BANDS]]
            for k, i in enumerate(found, 1)])

    @staticmethod
    def _mean_sd(row, band):
        m, sd = row[f"{band} mean dB"], row[f"{band} std dB"]
        if m is None or math.isnan(m):
            return "-"
        return f"{m:.1f}" + ("" if math.isnan(sd) else f" ({sd:.1f})")

    @staticmethod
    def _fill(table, rows):
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                table.setItem(r, c, QtWidgets.QTableWidgetItem(str(v)))

    # ---- update loop
    def _tick(self):
        x, self.cursor, lost = self.ring.since(self.cursor)
        self.lost += lost
        if x.size:
            self._process(x.astype(np.float64))
        self._update_status()
        if not self.paused:
            self._draw()

    def _process(self, x):
        env = self.envelope.process(x)
        keep = int(ENV_S * self.envelope.rate)
        self.env_hist = np.concatenate([self.env_hist, env])
        drop = max(0, self.env_hist.size - keep)
        self.env_hist = self.env_hist[drop:]
        self.env_index0 += drop

        if self.learning:
            done = self.env_index0 + self.env_hist.size - self.learn_from
            if done >= LEARN_S * self.envelope.rate:
                quiet = self.env_hist[self.learn_from - self.env_index0:]
                self.detector.learn(quiet)
                self.learning = False
        for ev in self.detector.process(env):
            self.events.append(ev)
            self.count_lbl.setText(str(len(self.events)))
            self.last_lbl.setText(f"last: {ev['snr_db']:.1f} dB, {ev['duration_s'] * 1000:.0f} ms")
            item = QtWidgets.QListWidgetItem(
                f"{time.strftime('%H:%M:%S')}  #{len(self.events):<3d} {ev['snr_db']:5.1f} dB  "
                f"{ev['duration_s'] * 1000:4.0f} ms")
            self.log.insertItem(0, item)

        x = np.concatenate([self._spec_rem, x])
        n = x.size // SPEC_N
        if n:
            blocks = x[: n * SPEC_N].reshape(n, SPEC_N)
            blocks = blocks - blocks.mean(axis=1, keepdims=True)
            mag = np.abs(np.fft.rfft(blocks * np.hanning(SPEC_N), axis=1))[:, : self.spec_bins]
            cols = 20 * np.log10(mag + 1e-9)
            self.spec_data = np.vstack([self.spec_data, cols])[-self.spec_cols:]
        self._spec_rem = x[n * SPEC_N:]

    def _update_status(self):
        link = self.link
        now = time.monotonic()
        self.rate_hist.append((now, self.ring.total))
        self.rate_hist = [(t, n) for t, n in self.rate_hist if now - t <= 3]
        (t0, n0), (t1, n1) = self.rate_hist[0], self.rate_hist[-1]
        rate = (n1 - n0) / (t1 - t0) if t1 > t0 else 0
        ovf = 0 if link.ovf_first is None else (link.ovf_last - link.ovf_first) & 0xFFFF
        self.status.setText(
            f"{link.status}   |   {rate:,.0f} samples/s (nominal {self.fs:,.0f})   |   "
            f"frames {link.frames:,}   |   CRC errors {link.parser.crc_errors}   |   "
            f"gaps {link.gaps} ({link.missing:,} samples)   |   overflows {ovf}")
        if self.learning:
            self.noise_lbl.setText(f"learning ({LEARN_S:g} s), keep still...")
        else:
            b = self.detector.baseline
            self.noise_lbl.setText(f"{b:.2f} counts RMS in {self.band_box.currentText()}; "
                                   f"threshold {self.thr.value():.0f} dB above it")

    def _draw(self):
        w = self.ring.latest(int(WAVE_S * self.fs)).astype(np.float64)
        if w.size > 64:
            if self.hp.isChecked():
                w = signal.sosfiltfilt(self.hp_sos, w)
            else:
                w = w - w.mean()
            self.wave_curve.setData((np.arange(w.size) - w.size) / self.fs, w)

        if self.env_hist.size and self.detector.baseline:
            rate = self.envelope.rate
            t = (np.arange(self.env_hist.size) - self.env_hist.size) / rate
            self.env_curve.setData(t, self.detector.level_db(self.env_hist))
            now_s = (self.env_index0 + self.env_hist.size) / rate
            pts = [(ev["t_s"] - now_s, ev["snr_db"]) for ev in self.events if now_s - ev["t_s"] <= ENV_S]
            self.event_scatter.setData([p[0] for p in pts], [p[1] for p in pts])
            self.env.setYRange(-10, max(40, self.thr.value() + 15), padding=0)

        img = self.spec_data
        if np.isfinite(img).any():
            levels = np.nanpercentile(img, [5, 99.5])
            self.spec_img.setImage(np.nan_to_num(img, nan=levels[0]), levels=levels, autoLevels=False)
            self.spec_img.setRect(self.spec_rect)  # the scale depends on the image size

    def shutdown(self):
        """Save any recording in progress and put the board back in stats mode."""
        if self.rec_btn.isChecked():
            self.rec_btn.setChecked(False)
        if self.source:
            self.source.stop()
            self.source.join(timeout=2)
            self.source = None

    def closeEvent(self, ev):
        self.shutdown()
        super().closeEvent(ev)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=SIM, help="COM port, or SIM for synthetic data")
    ap.add_argument("--fs-hz", type=float, default=NOMINAL_FS_HZ,
                    help="output rate of the flashed firmware build")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where Record saves trials")
    ap.add_argument("--snapshot", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--snapshot-after", type=float, default=15, help=argparse.SUPPRESS)
    args = ap.parse_args()

    pg.setConfigOptions(background="#0d1117", foreground="#d0d4d8")
    app = pg.mkQApp("VPU PCM")
    win = PcmDashboard(args)
    app.aboutToQuit.connect(win.shutdown)
    win.show()
    if args.snapshot:
        def snap():
            win.grab().save(args.snapshot)
            app.quit()
        QtCore.QTimer.singleShot(int(args.snapshot_after * 1000), snap)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
