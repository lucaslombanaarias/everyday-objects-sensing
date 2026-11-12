"""Live view and trial recorder for the VPU PCM stream (firmware/vpu_pcm_stream):
waveform (spectrogram and live level behind a toggle), live event preview, and per-trial
analysis with the det-v1 detection criterion (everyday_sensing.impacts).

    dashboard.bat                                              # double-click, in the repo root
    python tools/dashboard/vpu_pcm_dashboard.py                # finds the board by itself
    python tools/dashboard/vpu_pcm_dashboard.py --port COM3
    python tools/dashboard/vpu_pcm_dashboard.py --port SIM     # synthetic footsteps

The board is switched to binary mode while the window is open and back to stats mode when
it closes. Without --port the dashboard looks for the board (Espressif's USB ID) every
second until it is plugged in. The live preview learns its noise baseline from the first
3 s after connecting (keep still) or with the "Learn noise" button; it is a hysteresis
detector for watching the floor, not the det-v1 criterion the trials are analyzed with.

Trial tab: Notes and Record. Every other field is optional, under Details. Stop saves the
same CSV, JSON sidecar and .mat files as `python -m acquisition.record`, to
data/raw/<date>/ by default, then runs everyday_sensing.impacts on the folder and writes
its outputs to data/analysis/<date>/. Results tab: one trial's waveform with its detected
events, made against found, the events to tag, and a per-distance summary of every trial
in that folder. Recordings tab: every trial saved on this computer, to edit (notes and
details), trash, or share (scripts/share_trials.py: copy a day to data/controlled/, commit
and push).

Keys: Space = pause display, R = start/stop recording, N = learn noise, C = clear events.
Requires the repo's Python package (pip install -r requirements.txt from the repo root).
"""

import argparse
import dataclasses
import datetime
import json
import math
import re
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
    REPO_ROOT,
    apply_fields,
    assemble,
    build_meta,
    edit_trial,
    find_board_port,
    get_field,
    list_recordings,
    next_trial_id,
    parse_args,
    rename_trial,
    repo_state,
    trash_trial,
    write_trial,
)
from everyday_sensing import impacts
from everyday_sensing.events import BandEnvelope, EventDetector

SIM = "SIM"
AUTO = "AUTO"
"""--port value (the default) that looks for the board until it is plugged in."""
SHARE_SCRIPT = REPO_ROOT / "scripts" / "share_trials.py"
CONTROLLED = REPO_ROOT / "data" / "controlled"
"""Where shared trials go (scripts/share_trials.py)."""
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
NOISE = "noise only"
NOT_GIVEN = "not given"
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
        self.port = None        # the port streaming, once found and opened
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
            port = self.port
            if port == AUTO:
                port = find_board_port(serial.tools.list_ports.comports())
                if port is None:
                    self.link.status = "no board found: plug it in (looking every second)"
                    self.stop_evt.wait(1.0)
                    continue
            try:
                ser = serial.Serial()
                ser.port, ser.baudrate, ser.timeout = port, 115200, 0.05
                ser.dtr, ser.rts = True, False  # RTS high would hold the ESP32-C6 in reset
                ser.open()
            except (serial.SerialException, OSError) as e:
                hint = " (close Arduino Serial Monitor?)" if "denied" in str(e).lower() else ""
                self.link.status = f"{port}: cannot open{hint} - retrying"
                self.stop_evt.wait(1.0)
                continue
            self.link.port = port
            self.link.status = f"{port} streaming" + (
                " (found automatically)" if self.port == AUTO else "")
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
                self.link.status = f"{port}: disconnected - retrying"
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
        self.link.port = SIM
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


class DetailsForm(QtWidgets.QWidget):
    """The optional trial fields, on the Trial tab and in the Recordings tab's editor.
    Every field starts as not given; values() returns them named as in record.EDITABLE."""

    def __init__(self):
        super().__init__()
        self.type_box = QtWidgets.QComboBox()
        self.made = QtWidgets.QSpinBox(minimum=0, maximum=999, value=0,
                                       specialValueText=NOT_GIVEN)
        self.point_edit = QtWidgets.QLineEdit(placeholderText="e.g. P1")
        self.dist = spin(0, 100, 0, 0.5, decimals=2, suffix=" m")
        self.dist.setSpecialValueText(NOT_GIVEN)
        self.dist.setToolTip("Sensor radius: impact point to sensor center")
        self.height = spin(0, 500, 0, 5, decimals=1, suffix=" cm")
        self.height.setSpecialValueText(NOT_GIVEN)
        self.tip_edit = QtWidgets.QLineEdit(placeholderText="e.g. steel ball 16 mm")
        self.floor_edit = QtWidgets.QLineEdit(placeholderText="e.g. tile")
        self.mount_box = QtWidgets.QComboBox()
        self.block_box = QtWidgets.QComboBox(editable=True)
        self.block_box.lineEdit().setPlaceholderText("noise-only trial to measure against")
        self.operator_edit = QtWidgets.QLineEdit()
        self._choices(self.type_box, [*EXCITATIONS, NOISE])
        self._choices(self.mount_box, MOUNTS)
        self.form = QtWidgets.QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        for label, w in [("Excitation", self.type_box), ("Impacts made", self.made),
                         ("Excitation point", self.point_edit), ("Sensor radius", self.dist),
                         ("Drop height", self.height), ("Tip", self.tip_edit),
                         ("Floor", self.floor_edit), ("Mount", self.mount_box),
                         ("Baseline noise", self.block_box), ("Operator", self.operator_edit)]:
            self.form.addRow(QtWidgets.QLabel(label), w)

    @staticmethod
    def _choices(box, items):
        box.clear()
        box.addItem(NOT_GIVEN, None)
        for x in items:
            box.addItem(x, x)

    @staticmethod
    def _pick(box, value):
        """Select value, adding it when it is not one of the choices (older sidecars)."""
        if value in (None, ""):
            box.setCurrentIndex(0)
            return
        i = box.findData(value)
        if i < 0:
            box.addItem(str(value), value)
            i = box.count() - 1
        box.setCurrentIndex(i)

    def values(self) -> dict:
        kind = self.type_box.currentData()
        noise = kind == NOISE
        text = lambda edit: edit.text().strip() or None
        return {
            "noise_only": noise,
            "excitation.type": "none" if noise else kind,
            "impacts_made": 0 if noise else (self.made.value() or None),
            "excitation_point_id": text(self.point_edit),
            "distance_m": self.dist.value() or None,
            "excitation.drop_height_cm": self.height.value() or None,
            "excitation.tip": text(self.tip_edit),
            "floor_material": text(self.floor_edit),
            "sensor_mount.method": self.mount_box.currentData(),
            "baseline_noise_id": self.block_box.currentText().strip() or None,
            "operator": text(self.operator_edit),
        }

    def set_values(self, meta: dict):
        noise = impacts.is_noise(meta)
        self._pick(self.type_box, NOISE if noise else get_field(meta, "excitation.type"))
        self.made.setValue(0 if noise else (meta.get("impacts_made") or 0))
        self.point_edit.setText(meta.get("excitation_point_id") or "")
        self.dist.setValue(meta.get("distance_m") or 0)
        self.height.setValue(get_field(meta, "excitation.drop_height_cm") or 0)
        self.tip_edit.setText(get_field(meta, "excitation.tip") or "")
        self.floor_edit.setText(meta.get("floor_material") or "")
        self._pick(self.mount_box, get_field(meta, "sensor_mount.method"))
        self.block_box.setCurrentText(meta.get("baseline_noise_id") or "")
        self.operator_edit.setText(meta.get("operator") or "")


def safe_id(text: str) -> str:
    """A trial ID usable as a file name: spaces and other characters become _."""
    return re.sub(r"[^\w.-]+", "_", text.strip()).strip("_.")


def describe(meta: dict) -> str:
    """What a trial is, from whatever details it has: 'stomp, 1 m', 'noise only'."""
    if impacts.is_noise(meta):
        return NOISE
    kind = get_field(meta, "excitation.type")
    distance = meta.get("distance_m")
    parts = [kind or "no details"] + ([f"{distance:g} m"] if distance is not None else [])
    return ", ".join(parts)


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

        # Trial: notes and Record; every other field is optional, under Details
        self.notes_edit = QtWidgets.QPlainTextEdit(
            placeholderText="what this recording is, e.g. stomps 1 m from the sensor, tape mount")
        self.notes_edit.setFixedHeight(110)
        self.details = DetailsForm()
        fw = repo_state().get("fw_source_commit") or ""
        self.fw_edit = QtWidgets.QLineEdit(fw[:7], placeholderText="commit flashed on the board")
        self.prefix_edit = QtWidgets.QLineEdit(placeholderText="trial")
        self.trial_edit = QtWidgets.QLineEdit(placeholderText="filled in automatically")
        for label, w in [("Firmware commit", self.fw_edit), ("ID prefix", self.prefix_edit),
                         ("Trial ID", self.trial_edit)]:
            self.details.form.addRow(QtWidgets.QLabel(label), w)
        self.details.hide()
        details_btn = QtWidgets.QToolButton(text="Details (optional)", checkable=True)
        details_btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        details_btn.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        details_btn.setStyleSheet("QToolButton{border:none;color:#d0d4d8;font-size:14px}")

        def show_details(on):
            self.details.setVisible(on)
            details_btn.setArrowType(QtCore.Qt.ArrowType.DownArrow if on
                                     else QtCore.Qt.ArrowType.RightArrow)

        details_btn.toggled.connect(show_details)
        self.details_btn = details_btn
        for sig in (self.details.type_box.currentIndexChanged, self.details.dist.valueChanged,
                    self.prefix_edit.textChanged):
            sig.connect(lambda *_: self._suggest_trial_id())
        self.rec_btn = QtWidgets.QPushButton("● Record", checkable=True)
        self.rec_btn.setStyleSheet("font-size:18px;padding:10px")
        self.rec_btn.toggled.connect(self._set_recording)
        self.rec_lbl = QtWidgets.QLabel(" ", wordWrap=True, styleSheet="font-size:13px")
        intro = cap("Write what you are recording and press Record. Everything else is "
                    "optional, and the notes and details can be changed later in the "
                    "Recordings tab.")
        trial = page([intro, cap("Notes"), self.notes_edit, self.rec_btn, self.rec_lbl,
                      details_btn, self.details, QtWidgets.QWidget()])
        trial.layout().setStretch(trial.layout().count() - 1, 1)

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
        tabs.addTab(self._build_recordings(page, cap), "Recordings")
        tabs.addTab(live, "Live")
        self.tabs = tabs
        return tabs

    def _build_recordings(self, page, cap):
        """Recordings tab: every trial saved on this computer, its editor, and Share."""
        self.recordings = []
        self.share_proc = None
        self.rec_list = self._table(["day", "time", "trial", "length", "notes", "shared"])
        self.rec_list.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.rec_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.rec_list.horizontalHeader().setSectionResizeMode(
            4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.rec_list.setMinimumHeight(280)
        self.rec_list.itemSelectionChanged.connect(self._pick_recording)
        buttons = QtWidgets.QHBoxLayout()
        for text, slot in [("Refresh", lambda: self._refresh_recordings(reload_editor=True)),
                           ("Rename...", self._rename_recording),
                           ("Trash selected...", self._trash_recordings),
                           ("Open folder", self._open_recordings_folder)]:
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch()

        self.edit_title = QtWidgets.QLabel("select a recording to edit it", wordWrap=True,
                                           styleSheet="font-size:14px")
        self.edit_notes = QtWidgets.QPlainTextEdit()
        self.edit_notes.setFixedHeight(90)
        self.edit_details = DetailsForm()
        self.edit_loaded = {}
        self.save_btn = QtWidgets.QPushButton("Save changes")
        self.save_btn.clicked.connect(self._save_recording)
        self.edit_lbl = QtWidgets.QLabel(" ", wordWrap=True, styleSheet="font-size:13px")
        self.editor = page([self.edit_title, cap("Notes"), self.edit_notes, self.edit_details,
                            self.save_btn, self.edit_lbl])
        self.editor.layout().setContentsMargins(0, 0, 0, 0)
        self.editor.setEnabled(False)

        self.share_btn = QtWidgets.QPushButton("Share...")
        self.share_btn.setStyleSheet("font-size:15px;padding:6px")
        self.share_btn.clicked.connect(self._share)
        self.share_log = QtWidgets.QPlainTextEdit(readOnly=True)
        self.share_log.setStyleSheet("QPlainTextEdit{background:#0d1117;color:#d0d4d8;"
                                     "font-family:Consolas;font-size:12px}")
        self.share_log.setFixedHeight(150)
        self.share_log.hide()
        self.rec_folder_lbl = cap("")
        recordings = page([
            cap("Every recording saved on this computer, newest first. Select one to edit "
                "it; Ctrl- or Shift-click to select several to trash. Shared: copied to "
                "data/controlled/ (changed = edited since)."),
            self.rec_list, buttons, self.editor,
            cap("Share copies one day's recordings (.json and .mat) and their analysis to "
                "data/controlled/, commits them and pushes to GitHub, like "
                "scripts/share_trials.py. It shares the day of the selected recording."),
            self.share_btn, self.share_log, self.rec_folder_lbl])
        scroll = QtWidgets.QScrollArea(widgetResizable=True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(recordings)
        self._refresh_recordings()
        return scroll

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

    def _trial_taken(self, trial):
        stem = self._folder() / trial
        return any(stem.with_suffix(ext).exists() for ext in (".json", ".mat", ".csv"))

    def _suggest_trial_id(self):
        prefix = safe_id(self.prefix_edit.text()) or "trial"
        # Numbered after the day's shared trials too, which may come from another computer.
        shared = CONTROLLED / "raw" / self._folder().name
        self.trial_edit.setText(next_trial_id(self._folder(), prefix,
                                              self.details.type_box.currentData(),
                                              self.details.dist.value() or None, [shared]))

    def _fill_ports(self):
        self.port_box.clear()
        self.port_box.addItem("Find the board automatically", AUTO)
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
            taken = safe_id(self.trial_edit.text())
            if not taken or self._trial_taken(taken):
                self._suggest_trial_id()
                if taken:
                    note = f" ({taken} was already used)"
            else:
                self.trial_edit.setText(taken)
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
        # The recording is always saved: a taken ID gets the next free one.
        trial = safe_id(self.trial_edit.text())
        warning = ""
        if not trial or self._trial_taken(trial):
            self._suggest_trial_id()
            if trial:
                warning = f" (as {self.trial_edit.text()}, {trial} was already used)"
            trial = self.trial_edit.text()
        meta = self._trial_meta(trial, start, time.time() - self.rec_start)
        paths = write_trial(capture, meta, self.args.out_dir)
        verdict = "checks OK" if capture.checks["ok"] else "CHECKS FAILED (see .json)"
        kept = ("\nThe notes stay for the next recording: change them if it is different."
                if meta["notes"] else "")
        self.rec_lbl.setText(f"{verdict}, {capture.checks['samples']:,} samples{warning}\n"
                             f"saved {trial}.csv/.json/.mat in {paths['json'].parent}{kept}")
        self._suggest_trial_id()
        self._refresh_recordings()
        self._refresh_results(show=trial, switch_tab=True)

    def _trial_meta(self, trial, start, duration):
        """The sidecar for a recording: the recorder's fields, then the notes and whatever
        details the form gives (all optional)."""
        argv = ["--port", self.link.port or self.port, "--trial-id", trial,
                "--duration", f"{max(duration, 0.001):.3f}", "--fs-hz", str(self.fs)]
        if self.fw_edit.text().strip():
            argv += ["--fw-commit", self.fw_edit.text().strip()]
        notes = self.notes_edit.toPlainText().strip() or None
        return apply_fields(build_meta(parse_args(argv), start),
                            {**self.details.values(), "notes": notes})

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
            [r["excitation"] or "-", self._m(r["distance_m"]), r["trials"],
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
            self.trial_box.addItem(f"{meta['trial_id']}  ({describe(meta)})", meta["trial_id"])
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
        self._refresh_recordings(reload_editor=True)
        self.folder_lbl.setText(f"{trial} moved to {where}. " + self.folder_lbl.text())

    # ---- Recordings tab
    def _refresh_recordings(self, reload_editor=False):
        """Refill the list, keeping the selection. The editor is reloaded only when asked,
        so a recording saved meanwhile does not wipe edits that are not saved yet."""
        selected = {r["json"] for r in self._selected_recordings()}
        try:
            self.recordings = list_recordings(self.args.out_dir)
        except OSError as e:
            self.recordings = []
            self.rec_folder_lbl.setText(f"cannot read {self.args.out_dir}: {e}")
        table = self.rec_list
        table.blockSignals(True)
        table.clearSelection()
        table.setRowCount(len(self.recordings))
        colors = {"yes": COLORS["event"], "changed": COLORS["edge"]}
        for r, rec in enumerate(self.recordings):
            meta = rec["meta"]
            checks = meta.get("checks") or {}
            length = ("" if not checks.get("samples") or not meta.get("fs_hz")
                      else f"{checks['samples'] / meta['fs_hz']:.0f} s")
            notes = (meta.get("notes") or "").replace("\n", " ")
            cells = [rec["day"], (meta.get("start_time") or "")[11:19], meta["trial_id"],
                     length, notes if len(notes) <= 80 else notes[:77] + "...", rec["shared"]]
            for c, v in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(v)
                if c == 4:
                    item.setToolTip(meta.get("notes") or "")
                if c == 5 and v in colors:
                    item.setForeground(QtGui.QColor(colors[v]))
                table.setItem(r, c, item)
            if rec["json"] in selected:
                flags = QtCore.QItemSelectionModel.SelectionFlag
                table.selectionModel().select(table.model().index(r, 0),
                                              flags.Select | flags.Rows)
        table.blockSignals(False)
        self.rec_folder_lbl.setText(f"{len(self.recordings)} recordings in {self.args.out_dir}")
        if reload_editor:
            self._pick_recording()
        self._update_share_btn()

    def _selected_recordings(self):
        rows = sorted({i.row() for i in self.rec_list.selectedIndexes()})
        return [self.recordings[r] for r in rows if r < len(self.recordings)]

    def _pick_recording(self):
        rows = self._selected_recordings()
        self._update_share_btn()
        if len(rows) != 1:
            self.editor.setEnabled(False)
            self.edit_title.setText("select one recording to edit it" if not rows
                                    else f"{len(rows)} recordings selected")
            self.edit_lbl.setText(" ")
            return
        rec = rows[0]
        meta = rec["meta"]
        self.edit_title.setText(f"<b>{meta['trial_id']}</b>, {rec['day']} "
                                f"{(meta.get('start_time') or '')[11:19]} ({describe(meta)})")
        noise = [r["meta"]["trial_id"] for r in reversed(self.recordings)
                 if r["day"] == rec["day"] and impacts.is_noise(r["meta"])]
        box = self.edit_details.block_box
        box.clear()
        box.addItems(noise)
        self.edit_details.set_values(meta)
        self.edit_notes.setPlainText(meta.get("notes") or "")
        self.edit_loaded = self._edited_values()
        self.edit_lbl.setText(" ")
        self.editor.setEnabled(True)

    def _edited_values(self):
        return {**self.edit_details.values(),
                "notes": self.edit_notes.toPlainText().strip() or None}

    def _save_recording(self):
        rows = self._selected_recordings()
        if len(rows) != 1:
            return
        rec = rows[0]
        # Only what was changed in the editor, so fields it shows differently (older
        # sidecars, rounding) are left as they are.
        changes = {k: v for k, v in self._edited_values().items() if v != self.edit_loaded.get(k)}
        if not changes:
            self.edit_lbl.setText("nothing changed")
            return
        try:
            changed = edit_trial(rec["json"], changes)
        except (OSError, ValueError) as e:  # e.g. the .mat open in MATLAB
            QtWidgets.QMessageBox.warning(self, "Save changes", f"Could not save: {e}")
            return
        self._refresh_recordings(reload_editor=True)
        self.edit_lbl.setText(f"saved {', '.join(changed)}; the old values are kept in the "
                              "trial's edit_note")
        if rec["json"].parent.resolve() == self._folder().resolve():
            self._refresh_results(show=self.shown_id)

    def _rename_recording(self):
        rows = self._selected_recordings()
        if len(rows) != 1:
            QtWidgets.QMessageBox.information(self, "Rename recording",
                                              "Select one recording to rename it.")
            return
        rec = rows[0]
        old = rec["meta"]["trial_id"]
        copy = rec["shared_json"] if rec["shared"] in ("yes", "changed") else None
        prompt = f"New name for {old}:"
        if copy is not None:
            prompt += ("\n\nIt is shared already: the copy in data/controlled/ is renamed too, "
                       "and the next Share pushes the new name.")
        elif rec["shared"]:
            prompt += f"\n\nIt is shared already {rec['shared']}; that copy keeps its name."
        new, ok = QtWidgets.QInputDialog.getText(self, "Rename recording", prompt, text=old)
        new = safe_id(new)
        if not ok or not new or new == old:
            return
        # Another recording shared under that name (from the other computer, say) would be
        # overwritten by the next Share.
        other = CONTROLLED / "raw" / rec["day"] / f"{new}.json"
        if other.exists() and other != copy:
            theirs = json.loads(other.read_text(encoding="utf-8")).get("start_time")
            if theirs != rec["meta"].get("start_time"):
                QtWidgets.QMessageBox.warning(
                    self, "Rename recording", f"{new} is already shared for another recording "
                    f"from {rec['day']}. Pick another name.")
                return
        folder = rec["json"].parent
        try:
            new_json = rename_trial(rec["json"], new)
        except (OSError, ValueError) as e:  # taken, not a file name, or a file open elsewhere
            QtWidgets.QMessageBox.warning(self, "Rename recording", f"Not renamed: {e}")
            return
        note = ""
        try:
            impacts.rename_outputs(impacts.analysis_folder(folder), old, new)
        except OSError as e:
            note += f"; its analysis files could not be renamed: {e}"
        if copy is not None:
            try:
                rename_trial(copy, new)
                impacts.rename_outputs(impacts.analysis_folder(copy.parent), old, new)
                note += "; the shared copy too"
            except (OSError, ValueError) as e:
                note += f"; the shared copy could not be renamed: {e}"
        if self.shown_id == old:
            self.shown_id = new
        if folder.resolve() == self._folder().resolve():
            self._refresh_results(show=self.shown_id)
        elif impacts.analysis_folder(folder).is_dir():
            # The day-wide CSVs name every trial; analyze that day again to update them.
            try:
                impacts.write_outputs(
                    impacts.analyze_folder(folder, impacts.THRESHOLD_DB, self.analysis_cache),
                    impacts.analysis_folder(folder))
            except OSError as e:  # e.g. a CSV open in Excel
                note += f"; the day's analysis could not be updated: {e}"
        self.rec_list.clearSelection()
        self._refresh_recordings()
        row = next((r for r, x in enumerate(self.recordings) if x["json"] == new_json), None)
        if row is not None:
            self.rec_list.selectRow(row)
        self.edit_lbl.setText(f"renamed {old} to {new}{note}")

    def _trash_recordings(self):
        rows = self._selected_recordings()
        if not rows:
            return
        names = [r["meta"]["trial_id"] for r in rows]
        listed = "\n".join(names[:12]) + (f"\n... and {len(names) - 12} more" if len(names) > 12
                                          else "")
        shared = sum(bool(r["shared"]) for r in rows)
        also = (f"\n\n{shared} of them are shared already: this does not remove the copies in "
                "data/controlled/ or on GitHub." if shared else "")
        answer = QtWidgets.QMessageBox.question(
            self, "Trash recordings",
            f"Move {len(rows)} recording{'s' if len(rows) > 1 else ''} (.csv, .json, .mat) to "
            f"the Recycle Bin?\n\n{listed}\n\nYou can restore them from the Recycle Bin.{also}")
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        failed = []
        for r in rows:
            try:
                trash_trial(r["json"])
            except OSError as e:
                failed.append(f"{r['meta']['trial_id']}: {e}")
        if failed:
            QtWidgets.QMessageBox.warning(self, "Trash recordings", "\n".join(failed))
        self.rec_list.clearSelection()
        self._refresh_recordings(reload_editor=True)
        if self.shown_id in names:
            self.shown_id = None
        self._refresh_results(show=self.shown_id)

    def _open_recordings_folder(self):
        rows = self._selected_recordings()
        folder = rows[0]["json"].parent if rows else Path(self.args.out_dir)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))

    def _share_day(self):
        """The folder of the day to share: the selected recording's, else the newest."""
        rows = self._selected_recordings() or self.recordings[:1]
        return rows[0]["json"].parent if rows else None

    def _update_share_btn(self):
        day = self._share_day()
        running = self.share_proc is not None
        self.share_btn.setEnabled(day is not None and not running)
        self.share_btn.setText("Sharing..." if running else
                               "Share..." if day is None else f"Share {day.name} to GitHub...")

    def _share(self):
        day = self._share_day()
        if day is None or self.share_proc is not None:
            return
        on_day = [r for r in self.recordings if r["json"].parent == day]
        new = sum(not r["shared"] for r in on_day)
        answer = QtWidgets.QMessageBox.question(
            self, "Share recordings",
            f"Share the {len(on_day)} recordings from {day.name} ({new} not shared yet)?\n\n"
            "This copies their .json and .mat files and their analysis to data/controlled/, "
            "commits them, and pushes to GitHub (scripts/share_trials.py). Only "
            "data/controlled/ is committed.")
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        proc = QtCore.QProcess(self)
        proc.setProgram(sys.executable)
        proc.setArguments(["-u", str(SHARE_SCRIPT), str(day)])
        proc.setWorkingDirectory(str(REPO_ROOT))
        proc.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        env = QtCore.QProcessEnvironment.systemEnvironment()
        # No window to answer git in: fail rather than wait for a password or a merge message.
        env.insert("GIT_TERMINAL_PROMPT", "0")
        env.insert("GIT_MERGE_AUTOEDIT", "no")
        proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(self._share_output)
        proc.finished.connect(self._share_done)
        self.share_proc = proc
        self.share_log.clear()
        self.share_log.show()
        self.share_log.appendPlainText(f"> python scripts/share_trials.py {day}")
        self._update_share_btn()
        proc.start()

    def _share_output(self):
        text = bytes(self.share_proc.readAllStandardOutput()).decode(errors="replace")
        self.share_log.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        self.share_log.insertPlainText(text.replace("\r\n", "\n"))
        self.share_log.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _share_done(self, code, status):
        self._share_output()
        ok = status == QtCore.QProcess.ExitStatus.NormalExit and code == 0
        self.share_log.appendPlainText(
            "done" if ok else f"FAILED (exit code {code}): see the messages above")
        self.share_proc = None
        self._refresh_recordings()

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
        block_box = self.details.block_box
        text = block_box.currentText().strip()
        known, self.known_noise = self.known_noise, set(ids)
        if ids and (not text or ids[-1] not in known):
            text = ids[-1]
        block_box.clear()
        block_box.addItems(ids)
        block_box.setCurrentText(text)
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
            f"<b>{meta['trial_id']}</b>: {describe(meta)}, "
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
        if self.share_proc is not None:
            # Closing would stop git in the middle of the commit or push.
            QtWidgets.QMessageBox.information(
                self, "Sharing", "Sharing is still running; close the window when it is done.")
            ev.ignore()
            return
        self.shutdown()
        super().closeEvent(ev)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=AUTO,
                    help="COM port; AUTO (the default) finds the board, SIM gives synthetic data")
    ap.add_argument("--fs-hz", type=float, default=NOMINAL_FS_HZ,
                    help="output rate of the flashed firmware build")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="where Record saves trials")
    ap.add_argument("--snapshot", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--snapshot-after", type=float, default=15, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.port.upper() in (AUTO, SIM):
        args.port = args.port.upper()

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
