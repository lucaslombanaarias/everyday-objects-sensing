"""Live view and trial recorder for the VPU PCM stream (firmware/vpu_pcm_stream):
waveform (spectrogram and live level behind a toggle), live event preview, and per-trial
analysis with the det-v1 detection criterion (everyday_sensing.impacts).

    python tools/dashboard/vpu_pcm_dashboard.py --port COM3
    python tools/dashboard/vpu_pcm_dashboard.py --port SIM      # synthetic footsteps

The board is switched to binary mode while the window is open and back to stats mode when
it closes. The live preview learns its noise baseline from the first 3 s after connecting
(keep still) or with the "Learn noise" button; it is a hysteresis detector for watching the
floor, not the det-v1 criterion the trials are analyzed with.

Trial tab: fill in the trial (type, mount, impacts made, ...) and press Record; Record
refuses to start without the required fields. Stop saves the same CSV, JSON sidecar and
.mat files as `python -m acquisition.record`, to data/raw/<date>/ by default, then runs
everyday_sensing.impacts on the folder and writes its outputs to data/analysis/<date>/.
Results tab: one trial's waveform with its detected events, made against found, the
events to tag, and a per-distance summary of every trial in that folder.

Keys: Space = pause display, R = start/stop recording, N = learn noise, C = clear events.
Requires the repo's Python package (pip install -r requirements.txt from the repo root).
"""

import argparse
import dataclasses
import datetime
import json
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
    EXCITATIONS,
    MOUNTS,
    NOMINAL_FS_HZ,
    assemble,
    build_meta,
    next_trial_id,
    parse_args,
    repo_state,
    trash_trial,
    validate_meta,
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
# Live preview band options: (band, RMS window in s, envelope rate in Hz). The first has
# the det-v1 band and detection window (everyday_sensing.impacts); the live preview's
# hysteresis and baseline still differ from det-v1.
BANDS = {
    "above 100 Hz, 5 ms detection window": (impacts.DETECT_BAND, impacts.DETECT_WINDOW_S,
                                            impacts.DETECT_RATE_HZ),
    "100-500 Hz, 50 ms (feature)": ((100.0, 500.0), 0.05, 200.0),
    "2-8 kHz, 50 ms (resonance, feature)": ((2000.0, 8000.0), 0.05, 200.0),
}
COLORS = {"wave": "#4fc3f7", "env": "#ffb74d", "thr": "#ef5350", "event": "#66bb6a",
          "edge": "#ffa726", "skipped": "#b0b8c0"}
WAVE_FAINT = (79, 195, 247, 55)
"""The trial plot's waveform, drawn faint: the detector tests the RMS, not this."""
TRIAL_TYPES = [*EXCITATIONS, "noise only"]
DROPS = ("drop_stand", "ball_drop")
"""Excitations with a drop height and tip; walking has no single excitation point."""
AUTO_BASELINE = "automatic (latest noise-only trial before each trial)"
TRIAL_PLOT_H = 260
FALSE_ALARMS_HIGH = 3.0
"""False alarms per minute on a noise-only trial shown in orange; the floor procedure
(docs/testing/floor_procedure.md) stops a block above 3. The rate shown is det-v1 events
against the trial's own det-v1 baseline, over the whole recording: 1.3 to 2.0 per minute on
the 10/14 noise trials. The 1.3 to 4.1 noted on 10/14 measured each trial against its
quietest 2 s instead, over the recording after its first 1 s."""



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
        self.rec_first = None   # (wall time, samples) of the first batch recorded

    def on_frames(self, frames, ring):
        with self.lock:
            self.frames += len(frames)
            if self.recording is not None and not self.recording and frames:
                # The batch arrived now; its first sample was taken its duration earlier.
                self.rec_first = (time.time(), sum(f.samples.size for f in frames))
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
        self.shown_id = None
        self.shown_trial = None
        self.shown_events = []
        self.known_noise = set()
        self.trials_by_id = {}
        self.out_folder = impacts.analysis_folder(
            Path(args.out_dir) / datetime.date.today().isoformat())
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
        self.gap = spin(0.05, 5, impacts.MIN_GAP_S, 0.05, decimals=2, suffix=" s")
        for w in (self.band_box, self.thr, self.gap):
            w.setToolTip("Live preview only; trials are analyzed with det-v1")
        self.hp = QtWidgets.QCheckBox("High-pass 100 Hz", checked=True)
        self.more_live = QtWidgets.QCheckBox("Level + spectrogram")
        self.more_live.toggled.connect(self._show_more_live)
        learn = QtWidgets.QPushButton("Learn noise")
        learn.clicked.connect(self._learn)
        clear = QtWidgets.QPushButton("Clear events")
        clear.clicked.connect(self._clear)
        self.pause_btn = QtWidgets.QPushButton("Pause", checkable=True)
        self.pause_btn.toggled.connect(self._set_paused)
        for label, w in [("Port", self.port_box), ("Live band", self.band_box),
                         ("Live threshold", self.thr), ("Min gap", self.gap), (None, self.hp),
                         (None, self.more_live), (None, learn), (None, clear),
                         (None, self.pause_btn)]:
            if label:
                bar.addWidget(QtWidgets.QLabel(label))
            bar.addWidget(w)
        bar.addStretch()

        grid = QtWidgets.QGridLayout()
        root.addLayout(grid, 1)
        self.wave = pg.PlotWidget(
            title="Waveform (counts), live RMS level in the chosen band, threshold, events")
        self.wave.setLabel("bottom", "time", "s")
        self.wave.showGrid(x=True, y=True, alpha=0.2)
        self.wave_curve = self.wave.plot(pen=pg.mkPen(COLORS["wave"], width=1))
        self.wave_curve.setDownsampling(auto=True, method="peak")
        self.wave_curve.setClipToView(True)
        self.wave_env = self.wave.plot(pen=pg.mkPen(COLORS["env"], width=1.5))
        self.wave_thr = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(COLORS["thr"], width=1.5, style=QtCore.Qt.PenStyle.DashLine))
        self.wave.addItem(self.wave_thr)
        self.wave_events = pg.ScatterPlotItem(size=9, brush=pg.mkBrush(COLORS["event"]), pen=None)
        self.wave.addItem(self.wave_events)
        grid.addWidget(self.wave, 0, 0)

        self.env = pg.PlotWidget(title="Level above noise baseline (RMS in the chosen band)")
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
        self._show_more_live(False)

        grid.addWidget(self._build_panel(), 0, 1, 3, 1)
        grid.setColumnStretch(0, 5)
        grid.setColumnStretch(1, 3)

        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)
        QtGui.QShortcut(QtGui.QKeySequence("Space"), self, lambda: self.pause_btn.toggle())
        QtGui.QShortcut(QtGui.QKeySequence("R"), self, lambda: self.rec_btn.toggle())
        QtGui.QShortcut(QtGui.QKeySequence("N"), self, self._learn)
        QtGui.QShortcut(QtGui.QKeySequence("C"), self, self._clear)

    def _build_panel(self):
        tabs = QtWidgets.QTabWidget()
        tabs.setMinimumWidth(480)
        tabs.setStyleSheet("QWidget#tab{background:#161b22} QLabel{color:#d0d4d8} "
                           "QCheckBox{color:#d0d4d8}")
        cap = lambda text: QtWidgets.QLabel(text, styleSheet="font-size:14px;color:#8b949e",
                                            wordWrap=True)
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
        self.notes_by_type = {}
        self.last_type = self.type_box.currentText()
        self.point_edit = QtWidgets.QLineEdit(placeholderText="fixed impact point, e.g. P1")
        self.dist = spin(0, 100, 1.0, 0.5, decimals=2, suffix=" m")
        self.dist.setToolTip("Sensor radius: impact point to sensor center")
        self.height = spin(0, 500, 0, 5, decimals=1, suffix=" cm")
        self.height.setSpecialValueText("not given")
        self.tip_edit = QtWidgets.QLineEdit(placeholderText="optional, e.g. steel ball 16 mm")
        self.made = QtWidgets.QSpinBox(minimum=0, maximum=999, value=0,
                                       specialValueText="required")
        self.floor_edit = QtWidgets.QLineEdit(placeholderText="e.g. tile")
        # Mount starts blank, like Impacts made: Record refuses until one is picked, so a
        # trial is never saved with a default mount nobody chose.
        self.mount_box = QtWidgets.QComboBox(placeholderText="required")
        self.mount_box.addItems(MOUNTS)
        self.mount_box.setCurrentIndex(-1)
        self.block_box = QtWidgets.QComboBox(editable=True)
        self.block_box.lineEdit().setPlaceholderText("noise-only trial of this block")
        self.operator_edit = QtWidgets.QLineEdit()
        fw = repo_state().get("fw_source_commit") or ""
        self.fw_edit = QtWidgets.QLineEdit(fw[:7], placeholderText="commit flashed on the board")
        # Blank and required, like Mount: a default prefix named the 10/19 stomps "fs"
        # (footsteps).
        self.prefix_edit = QtWidgets.QLineEdit(placeholderText="required, e.g. tile_bare_drop")
        self.notes_edit = QtWidgets.QLineEdit(placeholderText="e.g. walking circle, 5 steps on marks")
        # Notes carry over between trials, so Record stops once when they are unchanged.
        self.last_notes = None          # (notes, trial id) of the last saved trial
        self.notes_confirmed = False
        self.notes_edit.textChanged.connect(self._notes_edited)
        self.trial_edit = QtWidgets.QLineEdit()
        form = QtWidgets.QFormLayout()
        for label, w in [("Excitation", self.type_box), ("Impacts made", self.made),
                         ("Excitation point", self.point_edit), ("Sensor radius", self.dist),
                         ("Drop height", self.height), ("Tip", self.tip_edit),
                         ("Floor", self.floor_edit), ("Mount", self.mount_box),
                         ("Baseline noise", self.block_box), ("Operator", self.operator_edit),
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
                    "of the trials after it. Excitation, Impacts made and Mount are required.")
        trial = page([intro, form, self.rec_btn,
                      self.rec_lbl, QtWidgets.QWidget()])
        trial.layout().setStretch(trial.layout().count() - 1, 1)
        self._type_changed()

        # Results: last trial and per-distance summary
        self.result_lbl = QtWidgets.QLabel("no trial recorded yet", wordWrap=True,
                                           styleSheet="font-size:15px")
        self.result_table = self._table(["#", "t (s)", "time", "dB", "flag", "tag"])
        self.result_table.setMinimumHeight(180)
        self.summary_table = self._table(["type", "radius", "n", "found / made", "diff.",
                                          "detection dB"])
        self.trial_plot, self.level_plot, self.spec_plot = self._trial_plots()
        self.more_box = QtWidgets.QCheckBox("Show detection level (dB) and spectrogram")
        self.more_box.toggled.connect(lambda _: self._draw_trial())
        self.folder_lbl = QtWidgets.QLabel(wordWrap=True, styleSheet="font-size:12px;color:#8b949e")
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(lambda: self._refresh_results(show=self.shown_id))
        self.baseline_box = QtWidgets.QComboBox()
        self.baseline_box.addItem(AUTO_BASELINE, None)
        self.baseline_box.currentIndexChanged.connect(
            lambda _: self._refresh_results(show=self.shown_id))
        baseline_row = QtWidgets.QHBoxLayout()
        baseline_row.addWidget(QtWidgets.QLabel("Baseline"))
        baseline_row.addWidget(self.baseline_box, 1)
        self.noise_table = self._table(["noise trial", "time", "level (counts)",
                                        "false alarms", "per min"])
        self.trial_box = QtWidgets.QComboBox()
        self.trial_box.currentIndexChanged.connect(lambda _: self._pick_trial())
        trash = QtWidgets.QPushButton("Trash...")
        trash.setToolTip("Move this trial's .csv, .json and .mat to the Recycle Bin")
        trash.clicked.connect(self._trash_shown)
        trial_row = QtWidgets.QHBoxLayout()
        trial_row.addWidget(self.trial_box, 1)
        trial_row.addWidget(trash)
        self.noise_table.setMaximumHeight(120)
        criterion = cap(impacts.DETECTION.describe())
        results = page([criterion, baseline_row,
                        cap("Noise-only trials: median RMS over the detection window, and "
                            "det-v1 events in them (false alarms)"),
                        self.noise_table, trial_row, self.result_lbl, self.trial_plot,
                        self.more_box, self.level_plot, self.spec_plot,
                        cap("Events: dB above the noise baseline; edge = within "
                            f"{impacts.EDGE_S:g} s of the start or end. Tags are saved to "
                            "events_<trial>.csv."),
                        self.result_table,
                        cap("All trials in the folder: mean (sd) detection dB per event"),
                        self.summary_table, self.folder_lbl, refresh])
        for box in (self.baseline_box, self.trial_box):
            # Long entries would otherwise set the tab's minimum width.
            box.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(16)
        scroll = QtWidgets.QScrollArea(widgetResizable=True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(results)
        results = scroll

        # Live: events from the band level plot
        self.count_lbl = QtWidgets.QLabel("0", alignment=center,
                                          styleSheet="font-size:64px;font-weight:bold")
        self.last_lbl = QtWidgets.QLabel(" ", alignment=center, styleSheet="font-size:18px")
        self.noise_lbl = QtWidgets.QLabel(styleSheet="font-size:14px", wordWrap=True)
        self.log = QtWidgets.QListWidget()
        self.log.setStyleSheet("QListWidget{background:#0d1117;color:#d0d4d8;font-family:Consolas;"
                               "font-size:13px;border:none}")
        preview = cap("Live preview: a hysteresis detector with a baseline learned at "
                      "connect, for watching the floor. Trials are analyzed with det-v1 "
                      "(Results tab).")
        live = page([preview, cap("Live events"), self.count_lbl, self.last_lbl,
                     cap("Live noise baseline"), self.noise_lbl, cap("Event log"), self.log])

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
        kind = self.type_box.currentText()
        # Notes are kept per type, so footstep notes do not end up on a noise trial.
        self.notes_by_type[self.last_type] = self.notes_edit.text()
        self.notes_edit.setText(self.notes_by_type.get(kind, ""))
        self.last_type = kind
        noise = kind == "noise only"
        for w in (self.dist, self.made):
            w.setEnabled(not noise)
        # Fields that do not apply are greyed out and left out of the trial; their values
        # stay in the form for when the excitation is switched back.
        self.point_edit.setEnabled(not noise and kind != "walking")
        for w in (self.height, self.tip_edit):
            w.setEnabled(kind in DROPS)

    def _trial_taken(self, trial):
        stem = self._folder() / trial
        return any(stem.with_suffix(ext).exists() for ext in (".json", ".mat", ".csv"))

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
        band, window, rate = BANDS[self.band_box.currentText()]
        self.envelope = BandEnvelope(self.fs, band, window_s=window, out_rate_hz=rate)
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
            note = ""
            taken = self.trial_edit.text().strip()
            if not taken or self._trial_taken(taken):
                self._suggest_trial_id()
                if taken:
                    note = f" ({taken} was already used)"
            problems = self._form_problems()
            if problems:
                # Refuse before anything is recorded, so no data is lost.
                self.rec_btn.blockSignals(True)
                self.rec_btn.setChecked(False)
                self.rec_btn.blockSignals(False)
                self.rec_lbl.setText("<span style='color:#ffa726'>not recording: "
                                     + "; ".join(problems) + "</span>")
                return
            notes = self.notes_edit.text().strip()
            if (self.last_notes and notes and notes == self.last_notes[0]
                    and not self.notes_confirmed):
                # Refuse once, so carried-over notes are read before they are saved again.
                self.notes_confirmed = True
                self.rec_btn.blockSignals(True)
                self.rec_btn.setChecked(False)
                self.rec_btn.blockSignals(False)
                self.notes_edit.setStyleSheet("border:2px solid #ffa726")
                self.rec_lbl.setText(
                    "<span style='color:#ffa726'>not recording: Notes are unchanged from "
                    f"{self.last_notes[1]}. Edit them, or press Record again to keep them."
                    "</span>")
                return
            self.notes_edit.setStyleSheet("")
            with link.lock:
                link.recording = []
                link.rec_first = None
                self.rec_counts = (link.parser.crc_errors, link.parser.skipped_bytes)
            self.rec_start = time.time()
            self.rec_btn.setText("■ Stop")
            self.rec_btn.setStyleSheet("font-size:18px;padding:10px;background:#b71c1c;color:white")
            self.rec_lbl.setText(f"recording {self.trial_edit.text().strip()}...{note}")
            return
        self.rec_btn.setText("● Record")
        self.rec_btn.setStyleSheet("font-size:18px;padding:10px")
        with link.lock:
            frames, link.recording = link.recording, None
            first = link.rec_first
            stats = types.SimpleNamespace(
                crc_errors=link.parser.crc_errors - self.rec_counts[0],
                skipped_bytes=link.parser.skipped_bytes - self.rec_counts[1])
        if not frames:
            self.rec_lbl.setText("nothing recorded")
            return
        t0 = frames[0].host_time_s
        frames = [dataclasses.replace(f, host_time_s=f.host_time_s - t0) for f in frames]
        capture = assemble(frames, stats, self.fs)
        # Start time of the first sample: arrival of the first batch less its duration.
        start_s = first[0] - first[1] / self.fs if first else self.rec_start
        start = datetime.datetime.fromtimestamp(start_s).astimezone()
        # The recording is always saved: a taken ID gets the next free one, and a form the
        # recorder rejects is saved with the basic fields only.
        trial = self.trial_edit.text().strip()
        warning = ""
        if self._trial_taken(trial):
            self._suggest_trial_id()
            warning = f" (as {self.trial_edit.text()}, {trial} was already used)"
            trial = self.trial_edit.text()
        try:
            args = parse_args(self._record_argv(trial))
        except SystemExit:
            args = parse_args(self._record_argv(trial)[:8] + ["--notes", "form rejected: "
                                                               + self.notes_edit.text()])
            warning += " (form rejected, saved with basic fields only)"
        meta = build_meta(args, start)
        if problems := validate_meta(meta):
            warning += f" (saved, but incomplete: {'; '.join(problems)})"
        paths = write_trial(capture, meta, self.args.out_dir)
        self.last_notes = (self.notes_edit.text().strip(), trial)
        self.notes_confirmed = False
        verdict = "checks OK" if capture.checks["ok"] else "CHECKS FAILED (see .json)"
        self.rec_lbl.setText(f"{verdict}, {capture.checks['samples']:,} samples{warning}\n"
                             f"saved {trial}.csv/.json/.mat in {paths['json'].parent}")
        self._suggest_trial_id()
        self._refresh_results(show=trial, switch_tab=True)

    def _notes_edited(self):
        self.notes_confirmed = False
        self.notes_edit.setStyleSheet("")

    def _record_argv(self, trial, duration=None):
        duration = time.time() - self.rec_start if duration is None else duration
        argv = ["--port", self.port, "--trial-id", trial,
                "--duration", f"{duration:.3f}", "--fs-hz", str(self.fs)]
        kind = self.type_box.currentText()
        if kind == "noise only":
            argv.append("--noise-only")
        else:
            argv += ["--excitation-type", kind, "--distance-m", f"{self.dist.value():g}"]
            if self.made.value():
                argv += ["--impacts", str(self.made.value())]
            if self.height.isEnabled() and self.height.value():
                argv += ["--drop-height-cm", f"{self.height.value():g}"]
            for flag, edit in [("--excitation-point", self.point_edit), ("--tip", self.tip_edit)]:
                if edit.isEnabled() and edit.text().strip():
                    argv += [flag, edit.text().strip()]
        if self.mount_box.currentText():
            argv += ["--mount-method", self.mount_box.currentText()]
        if self.block_box.currentText().strip():
            argv += ["--baseline-noise-id", self.block_box.currentText().strip()]
        for flag, edit in [("--floor-material", self.floor_edit),
                           ("--operator", self.operator_edit), ("--fw-commit", self.fw_edit),
                           ("--notes", self.notes_edit)]:
            if edit.text().strip():
                argv += [flag, edit.text().strip()]
        return argv

    def _form_problems(self):
        """What the form is missing for a trial (validate_meta), checked before Record."""
        try:
            args = parse_args(self._record_argv(self.trial_edit.text().strip() or "t", 1.0))
        except SystemExit:
            return ["the recorder rejected the form"]
        problems = [] if self.prefix_edit.text().strip() else [
            "ID prefix is required: one per condition, e.g. tile_bare_drop"]
        return problems + validate_meta({
            "noise_only": args.noise_only,
            "sensor_mount": {"method": args.mount_method},
            "excitation": {"type": args.excitation_type},
            "impacts_made": args.impacts,
        })

    def _refresh_results(self, show=None, switch_tab=False):
        folder = self._folder()
        self._suggest_trial_id()
        if not folder.exists():
            self.folder_lbl.setText(f"{folder} (no trials yet)")
            return
        chosen = self._sync_baselines(folder)
        # Always det-v1: the live threshold and band do not change the analysis.
        trials = impacts.analyze_folder(folder, impacts.THRESHOLD_DB, self.analysis_cache, chosen)
        self.out_folder = impacts.analysis_folder(folder, impacts.DETECTION, chosen)
        note = ""
        try:
            impacts.write_outputs(trials, self.out_folder)
        except OSError as e:  # e.g. an events CSV open in Excel
            note = f"; COULD NOT WRITE THE OUTPUTS: {e}"
        noises = [t for t in trials if t["result"] is None]
        rows = []
        for t in noises:
            row = impacts.count_row(t, impacts.event_rows(t))
            rows.append([t["meta"]["trial_id"], t["meta"].get("start_time", "")[11:19],
                         f"{t['noise_baseline'][impacts.DETECT]:.2f}", row["impacts_found"],
                         f"{row['false_alarms_per_min']:.1f}"])
        self._fill(self.noise_table, rows)
        for r, row in enumerate(rows):
            if float(row[4]) > FALSE_ALARMS_HIGH:
                self.noise_table.item(r, 4).setForeground(QtGui.QColor(COLORS["edge"]))
        self.folder_lbl.setText(f"{folder}, {len(trials)} trials, {impacts.DETECTION.version}, "
                                f"baseline {chosen or 'automatic'}; outputs in "
                                f"{self.out_folder}{note}")
        rows = impacts.summarize(trials)
        self._fill(self.summary_table, [
            [r["excitation"], self._m(r["distance_m"]), r["trials"],
             f"{r['impacts_found']} / {'-' if r['impacts_made'] is None else r['impacts_made']}",
             "-" if r["impacts_made"] is None else f"{r['impacts_found'] - r['impacts_made']:+d}",
             self._mean_sd(r, impacts.DETECT)] for r in rows])
        shown = next((t for t in trials if t["meta"]["trial_id"] == show), None)
        if shown is None:
            shown = trials[-1] if trials else None
        if shown is not None:
            self.shown_id = shown["meta"]["trial_id"]
            self._show_trial(shown)
        self.trials_by_id = {t["meta"]["trial_id"]: t for t in trials}
        self.trial_box.blockSignals(True)
        self.trial_box.clear()
        for t in reversed(trials):
            meta = t["meta"]
            kind = "noise only" if t["result"] is None else (
                f"{meta['excitation']['type']}, {self._m(meta.get('distance_m'))}")
            self.trial_box.addItem(f"{meta['trial_id']}  ({kind})", meta["trial_id"])
        self.trial_box.setCurrentIndex(max(0, self.trial_box.findData(self.shown_id)))
        self.trial_box.blockSignals(False)
        if shown is not None and switch_tab:
            self.tabs.setCurrentIndex(1)

    def _pick_trial(self):
        t = self.trials_by_id.get(self.trial_box.currentData())
        if t is not None:
            self.shown_id = t["meta"]["trial_id"]
            self._show_trial(t)

    def _trash_shown(self):
        trial = self.trial_box.currentData()
        if not trial:
            return
        path = self._folder() / f"{trial}.json"
        answer = QtWidgets.QMessageBox.question(
            self, "Trash trial",
            f"Move {trial} (.csv, .json, .mat) to the Recycle Bin?\n\n"
            "You can restore it from the Recycle Bin. It is removed from the results.")
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            where = trash_trial(path)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Trash trial", str(e))
            return
        self.shown_id = None
        self._refresh_results()
        self.folder_lbl.setText(f"{trial} moved to {where}. " + self.folder_lbl.text())

    def _sync_baselines(self, folder):
        """Refill the baseline dropdown with the folder's noise-only trials, keeping the
        selection if that trial still exists. Returns the chosen trial id (None = auto)."""
        current = self.baseline_box.currentData()
        ids = []
        for path in impacts.trial_jsons(folder):
            meta = json.loads(path.read_text(encoding="utf-8"))
            if impacts.is_noise(meta):
                ids.append(meta["trial_id"])
        self.baseline_box.blockSignals(True)
        self.baseline_box.clear()
        self.baseline_box.addItem(AUTO_BASELINE, None)
        for trial_id in ids:
            self.baseline_box.addItem(trial_id, trial_id)
        idx = self.baseline_box.findData(current) if current in ids else 0
        self.baseline_box.setCurrentIndex(idx)
        self.baseline_box.blockSignals(False)
        # The form's block baseline follows the newest noise-only trial when one is added.
        text = self.block_box.currentText().strip()
        known, self.known_noise = self.known_noise, set(ids)
        if ids and (not text or ids[-1] not in known):
            text = ids[-1]
        self.block_box.clear()
        self.block_box.addItems(ids)
        self.block_box.setCurrentText(text)
        return self.baseline_box.currentData()

    def _show_trial(self, t):
        meta = t["meta"]
        self.shown_trial = t
        events = (impacts.read_events(self.out_folder / f"events_{meta['trial_id']}.csv")
                  or impacts.event_rows(t))
        row = impacts.count_row(t, events)
        orange = "<span style='color:#ffa726'>{}</span>"
        tagged = "" if row["unreviewed"] == row["impacts_found"] else ", tagged: " + ", ".join(
            f"{row[tag]} {tag}" for tag in impacts.TAGS if row[tag])
        if t["result"] is None:
            rate = f"{row['false_alarms_per_min']:.1f} per minute"
            if row["false_alarms_per_min"] > FALSE_ALARMS_HIGH:
                rate = orange.format(rate)
            self.result_lbl.setText(
                f"<b>{meta['trial_id']}</b>: noise only, mount {row['mount'] or '-'}<br>"
                f"<b>{row['impacts_found']} false alarms</b> in {row['duration_s']:.0f} s "
                f"({rate})"
                + (f", {row['edge_events']} at the edges" if row["edge_events"] else "")
                + f"{tagged}<br>baseline: itself ({t['noise_baseline'][impacts.DETECT]:.2f} "
                f"counts); it is the baseline for the trials recorded after it.")
            self.shown_events = events
            self._fill_events(events)
            self._draw_trial()
            return
        if row["difference"] is None:
            counts = f"<b>{row['impacts_found']} found</b>, impacts made not recorded"
        else:
            diff = f"difference {row['difference']:+d}"
            counts = (f"made {row['impacts_made']}, <b>found {row['impacts_found']}</b>, "
                      + (diff if row["difference"] == 0 else orange.format(diff)))
        if row["edge_events"]:
            counts += f", {row['edge_events']} at the edges"
        counts += tagged
        traces = self._traces(t)
        skipped = [] if traces is None else impacts.skipped_peaks(traces[0])
        if skipped:
            counts += (f"<br>{len(skipped)} peak{'s' if len(skipped) > 1 else ''} below the "
                       f"threshold (hollow): " + ", ".join(
                           f"{when:.2f} s {db:.1f} dB" for when, db in skipped))
        merged = [] if traces is None else impacts.merged_peaks(
            traces[0], [float(e["t_s"]) for e in events])
        if merged:
            counts += (f"<br>{len(merged)} burst{'s' if len(merged) > 1 else ''} merged into a "
                       f"stronger event within {impacts.MIN_GAP_S:g} s (hollow square): "
                       + ", ".join(f"{when:.2f} s" for when, _ in merged))
        baseline = row["baseline_used"] + (
            orange.format(f" (the trial records {row['baseline_recorded']})")
            if row["baseline_mismatch"] else "")
        self.result_lbl.setText(
            f"<b>{meta['trial_id']}</b>: {row['excitation']} at {self._m(row['distance_m'])}, "
            f"point {row['excitation_point_id'] or '-'}, mount {row['mount'] or '-'}<br>"
            f"{counts}<br>baseline: {baseline} "
            f"({t['result']['baseline'][impacts.DETECT]:.2f} counts)")
        self.shown_events = events
        self._fill_events(events)
        self._draw_trial()

    def _fill_events(self, rows):
        table = self.result_table
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            cells = [row["event"], f"{float(row['t_s']):.3f}", str(row["timestamp"])[11:23],
                     f"{float(row['peak_db']):.1f}", row["flag"]]
            for c, v in enumerate(cells):
                table.setItem(r, c, QtWidgets.QTableWidgetItem(str(v)))
            box = QtWidgets.QComboBox()
            box.addItems([impacts.UNREVIEWED, *impacts.TAGS])
            box.setCurrentText(row["annotation"])
            box.currentTextChanged.connect(lambda tag, k=int(row["event"]): self._tag(k, tag))
            table.setCellWidget(r, 5, box)

    def _tag(self, event, tag):
        path = self.out_folder / f"events_{self.shown_id}.csv"
        try:
            impacts.save_tags(path, {event: tag})
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Save tag", f"Could not save the tag: {e}")
            return
        # Refresh after this signal returns, since it rebuilds the table holding the box.
        QtCore.QTimer.singleShot(0, lambda: self._refresh_results(show=self.shown_id))

    # ---- trial plot
    def _trial_plots(self):
        # Short enough for the side panel; the README explains the markers.
        wave = pg.PlotWidget(title="5 ms RMS (orange), threshold, events")
        wave.setLabel("bottom", "time from start", "s")
        wave.setLabel("left", "counts")
        wave.showGrid(x=True, y=True, alpha=0.2)
        wave.setMinimumHeight(TRIAL_PLOT_H)
        level = pg.PlotWidget(title="Detection level (dB above the noise baseline)")
        level.setLabel("bottom", "time from start", "s")
        level.showGrid(x=True, y=True, alpha=0.2)
        spec = pg.PlotWidget(title="Spectrogram (dB)")
        spec.setLabel("bottom", "time from start", "s")
        spec.setLabel("left", "frequency", "Hz")
        for p in (level, spec):
            p.setMinimumHeight(200)
            p.hide()
        return wave, level, spec

    def _traces(self, t):
        """detection_traces for the shown trial, kept for the last trial drawn."""
        meta = t["meta"]
        baseline = t["noise_baseline"] if t["result"] is None else t["result"]["baseline"]
        key = (meta["trial_id"], meta.get("start_time"), baseline[impacts.DETECT])
        if getattr(self, "_trace_key", None) != key:
            try:
                _, x, fs = impacts.load_trial(self._folder() / f"{meta['trial_id']}.json")
            except Exception:  # trashed or still being written
                return None
            self._trace = (impacts.detection_traces(x, fs, baseline), x, fs)
            self._trace_key = key
        return self._trace

    def _draw_trial(self):
        more = self.more_box.isChecked()
        self.level_plot.setVisible(more)
        self.spec_plot.setVisible(more)
        for p in (self.trial_plot, self.level_plot, self.spec_plot):
            p.clear()
        t = getattr(self, "shown_trial", None)
        found = None if t is None else self._traces(t)
        if found is None:
            return
        tr, x, fs = found
        thr_pen = pg.mkPen(COLORS["thr"], width=1.5, style=QtCore.Qt.PenStyle.DashLine)
        # The detector compares the 5 ms RMS with the threshold, so the RMS is drawn bold and
        # the waveform faint: a sharp peak in the waveform can cross the line while its RMS
        # stays below it.
        for xs, ys, pen in [(tr["t_s"], tr["highpassed"], pg.mkPen(WAVE_FAINT, width=1)),
                            (tr["rms_t_s"], tr["rms"], pg.mkPen(COLORS["env"], width=2.5))]:
            curve = self.trial_plot.plot(xs, ys, pen=pen)
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
        self.trial_plot.addItem(pg.InfiniteLine(
            tr["threshold_counts"], angle=0, pen=thr_pen,
            label=f"threshold {impacts.THRESHOLD_DB:g} dB (5 ms RMS)",
            labelOpts={"color": COLORS["thr"], "position": 0.02,
                       "anchors": [(0, 1), (0, 1)]}))  # left-aligned, above the line
        times = [float(e["t_s"]) for e in self.shown_events]
        dbs = [float(e["peak_db"]) for e in self.shown_events]
        brushes = [pg.mkBrush(COLORS["edge"] if e["flag"] == "edge" else COLORS["event"])
                   for e in self.shown_events]
        peaks = [tr["baseline"] * 10 ** (db / 20) for db in dbs]
        self.trial_plot.addItem(pg.ScatterPlotItem(times, peaks, size=10, brush=brushes, pen=None))
        for e, when, peak in zip(self.shown_events, times, peaks):
            label = pg.TextItem(str(e["event"]), color=COLORS["event"], anchor=(0.5, 1.2))
            label.setPos(when, peak)
            self.trial_plot.addItem(label)
        # Skipped peaks: hollow, with their level, so a miss shows why it was missed.
        skipped = impacts.skipped_peaks(tr)
        hollow = dict(size=11, brush=None, pen=pg.mkPen(COLORS["skipped"], width=1.5))
        sk_t = [when for when, _ in skipped]
        sk_db = [db for _, db in skipped]
        sk_counts = [tr["baseline"] * 10 ** (db / 20) for db in sk_db]
        self.trial_plot.addItem(pg.ScatterPlotItem(sk_t, sk_counts, **hollow))
        for when, db, peak in zip(sk_t, sk_db, sk_counts):
            label = pg.TextItem(f"{db:.1f} dB", color=COLORS["skipped"], anchor=(0.5, 1.3))
            label.setPos(when, peak)
            self.trial_plot.addItem(label)
        # Bursts above the threshold merged into a stronger event within the minimum gap.
        merged = impacts.merged_peaks(tr, times)
        mg_t = [when for when, _ in merged]
        mg_db = [db for _, db in merged]
        mg_counts = [tr["baseline"] * 10 ** (db / 20) for db in mg_db]
        square = dict(hollow, symbol="s")
        self.trial_plot.addItem(pg.ScatterPlotItem(mg_t, mg_counts, **square))
        for when, peak in zip(mg_t, mg_counts):
            label = pg.TextItem("merged", color=COLORS["skipped"], anchor=(0.5, 1.3))
            label.setPos(when, peak)
            self.trial_plot.addItem(label)
        if not more:
            return
        self.level_plot.plot(tr["rms_t_s"], tr["level_db"], pen=pg.mkPen(COLORS["env"], width=1.5))
        self.level_plot.addItem(pg.InfiniteLine(impacts.THRESHOLD_DB, angle=0, pen=thr_pen))
        self.level_plot.addItem(pg.ScatterPlotItem(times, dbs, size=9, brush=brushes, pen=None))
        self.level_plot.addItem(pg.ScatterPlotItem(sk_t, sk_db, **hollow))
        self.level_plot.addItem(pg.ScatterPlotItem(mg_t, mg_db, **square))
        if x.size >= SPEC_N:
            f, ts, sxx = signal.spectrogram(x - np.mean(x), fs, nperseg=SPEC_N,
                                            noverlap=SPEC_N // 2)
            keep = f <= SPEC_FMAX
            img = pg.ImageItem(10 * np.log10(sxx[keep].T + 1e-12))
            img.setColorMap(pg.colormap.get("viridis"))
            img.setRect(QtCore.QRectF(ts[0], 0, ts[-1] - ts[0], f[keep][-1]))
            self.spec_plot.addItem(img)

    def _show_more_live(self, on):
        self.env.setVisible(on)
        self.spec.setVisible(on)

    @staticmethod
    def _m(distance):
        return "-" if distance is None else f"{distance:g} m"

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
            now_s = (self.env_index0 + self.env_hist.size) / rate
            recent = t >= -WAVE_S
            self.wave_env.setData(t[recent], self.env_hist[recent])
            self.wave_thr.setValue(self.detector.baseline * 10 ** (self.thr.value() / 20))
            pts = [(ev["t_s"] - now_s, ev["peak"]) for ev in self.events if now_s - ev["t_s"] <= WAVE_S]
            self.wave_events.setData([p[0] for p in pts], [p[1] for p in pts])
            if self.env.isVisible():
                self.env_curve.setData(t, self.detector.level_db(self.env_hist))
                pts = [(ev["t_s"] - now_s, ev["snr_db"]) for ev in self.events if now_s - ev["t_s"] <= ENV_S]
                self.event_scatter.setData([p[0] for p in pts], [p[1] for p in pts])
                self.env.setYRange(-10, max(40, self.thr.value() + 15), padding=0)

        if not self.spec.isVisible():
            return
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
