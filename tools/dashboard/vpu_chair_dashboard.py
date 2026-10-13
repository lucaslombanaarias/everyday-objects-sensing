"""Chair occupancy demo for the VPU board: live vibration + sit-down / stand-up events.

Reads the wired VPU_HighRate_View sketch over USB serial, one line per sample:

    vibration:12.3<TAB>floor:0<TAB>ceiling:100

(any line with a `vibration` field works; otherwise the first number is used).

Two detection modes (dropdown in the toolbar, or --mode):

  sit     (default) A vibration burst on an EMPTY chair is a sit-down -> OCCUPIED.
          Bursts while OCCUPIED are logged as seated movement and change nothing.
          Stand-ups are too quiet to rely on with some chairs, so press
          "Reset for next person" (N) after each person to go back to EMPTY.
  toggle  Every burst flips EMPTY <-> OCCUPIED (sit-down, then stand-up).

After an event, new events are locked out until the chair has been quiet for a
moment, so settling/shuffling doesn't count twice. If the state gets out of sync,
click "Set empty" / "Set occupied".

    python vpu_chair_dashboard.py --port COM5
    python vpu_chair_dashboard.py --port COM5 --mode toggle
    python vpu_chair_dashboard.py --port SIM        # synthetic chair, no hardware

Keys: Space = pause display, R = start/stop CSV recording, E / O = set empty / occupied,
      N = reset for the next person (EMPTY, clear log).

Default threshold 15: in test recordings stand-ups peaked as low as 17-24 while the
empty chair stayed below ~5 (rare blips to ~10).
Serial parsing and the reconnecting reader are shared with live_dashboard.py.
"""

import argparse
import collections
import csv
import math
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import serial.tools.list_ports
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from live_dashboard import SIM, SerialSource, Stream, spin

TICK_MS = 33             # UI + detector update period
TRACE_S = 20             # seconds of raw vibration shown
HISTORY_S = 180          # seconds of occupancy history shown
NOMINAL_FS = 200.0       # used until the real rate has been measured
SIT, STAND, MOVE = "SAT DOWN", "STOOD UP", "movement (seated)"
EVENT_COLORS = {SIT: "#66bb6a", STAND: "#ffa726", MOVE: "#8b949e"}
MARKER_TEXT = {SIT: "SIT", STAND: "STAND", MOVE: ""}
MODES = {"sit": "Sit-down only", "toggle": "Sit + stand"}


class ChairSim(threading.Thread):
    """Synthetic VPU score: sit at 3 s, stand at 15 s of every 24 s, fidgets in between."""

    def __init__(self, stream, fs=200.0):
        super().__init__(daemon=True)
        self.stream, self.fs = stream, fs
        self.stop_evt = threading.Event()

    def run(self):
        self.stream.status = f"SIM (chair, {self.fs:g} Hz)"
        rng = np.random.default_rng(1)
        n0, t0 = 0, time.monotonic()
        while not self.stop_evt.wait(0.02):
            due = int((time.monotonic() - t0) * self.fs)
            if due <= n0:
                continue
            t = np.arange(n0, due) / self.fs
            n0 = due
            c = t % 24

            def burst(at, peak, tau):
                dt = c - at
                return np.where(dt >= 0, peak * np.exp(-dt / tau) * np.abs(np.sin(2 * np.pi * 7 * dt + 0.6)), 0)

            v = (burst(3.0, 100, 0.45) + burst(3.9, 22, 0.2)      # sit down + settle
                 + burst(8.0, 14, 0.25) + burst(11.5, 18, 0.25)    # fidgets while seated
                 + burst(15.0, 80, 0.7))                           # stand up
            v += np.where(rng.random(t.size) < 0.02, rng.uniform(2, 6, t.size), 0)
            v = np.clip(v, 0, 100)
            v[v < 2] = 0
            self.stream.feed([f"vibration:{x:.1f}\tfloor:0\tceiling:100" for x in v])

    def stop(self):
        self.stop_evt.set()


class ChairDashboard(QtWidgets.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.stream = Stream()
        self.source = None
        self.paused = False
        self.rec_file = None
        self.rate_hist = collections.deque()
        self.fs = None
        self.occupied = args.start_occupied
        self.state_since = time.time()
        self._reset_detector()
        self.history = collections.deque()          # (wall time, envelope max, occupied)
        self.markers = []                           # (sample index, kind, InfiniteLine)
        self.counts = {SIT: 0, STAND: 0}
        self.banner_until = 0.0

        self.setWindowTitle(args.title)
        self.resize(1500, 900)
        self._build_ui()
        self._start_source(args.port)
        self._show_state()
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
        refresh = QtWidgets.QPushButton("↻")
        refresh.setFixedWidth(32)
        refresh.clicked.connect(self._fill_ports)
        self.thr = spin(1, 100, self.args.threshold, 1, decimals=0)
        self.lockout = spin(0, 30, 2.5, 0.5, suffix=" s")
        self.decay = spin(0.05, 5, 0.4, 0.05, decimals=2, suffix=" s")
        self.mode_box = QtWidgets.QComboBox()
        for key, text in MODES.items():
            self.mode_box.addItem(text, key)
        self.mode_box.setCurrentIndex(list(MODES).index(self.args.mode))
        empty_btn = QtWidgets.QPushButton("Set empty")
        empty_btn.clicked.connect(lambda: self._set_state(False, manual=True))
        occ_btn = QtWidgets.QPushButton("Set occupied")
        occ_btn.clicked.connect(lambda: self._set_state(True, manual=True))
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._set_paused)
        self.rec_btn = QtWidgets.QPushButton("● Record")
        self.rec_btn.setCheckable(True)
        self.rec_btn.toggled.connect(self._set_recording)
        for label, w in [("Port", self.port_box), (None, refresh), ("Mode", self.mode_box),
                         ("Event threshold", self.thr),
                         ("Quiet lockout", self.lockout), ("Envelope decay", self.decay),
                         (None, empty_btn), (None, occ_btn), (None, self.pause_btn), (None, self.rec_btn)]:
            if label:
                bar.addWidget(QtWidgets.QLabel(label))
            bar.addWidget(w)
        bar.addStretch()

        grid = QtWidgets.QGridLayout()
        root.addLayout(grid, 1)

        self.trace = pg.PlotWidget(title="Chair vibration (VPU score)")
        self.trace.setLabel("bottom", "time", "s")
        self.trace.setYRange(0, 105, padding=0)
        self.trace.setXRange(-TRACE_S, 0, padding=0)
        self.trace.showGrid(x=True, y=True, alpha=0.2)
        self.trace.setMouseEnabled(x=False, y=False)
        self.env_curve = self.trace.plot(pen=None, fillLevel=0, brush=(79, 195, 247, 50))
        self.raw_curve = self.trace.plot(pen=pg.mkPen("#4fc3f7", width=1.5))
        self.thr_line = pg.InfiniteLine(pos=self.thr.value(), angle=0, movable=True,
                                        pen=pg.mkPen("#ef5350", width=2, style=QtCore.Qt.PenStyle.DashLine),
                                        label="threshold", labelOpts=dict(position=0.03, color="#ef5350"))
        self.trace.addItem(self.thr_line)
        self.thr_line.sigPositionChanged.connect(lambda: self.thr.setValue(self.thr_line.value()))
        self.thr.valueChanged.connect(lambda v: self.thr_line.setValue(v))
        grid.addWidget(self.trace, 0, 0)

        self.hist = pg.PlotWidget(title="Occupancy history")
        self.hist.setLabel("bottom", "minutes ago")
        self.hist.setYRange(0, 105, padding=0)
        self.hist.setXRange(-HISTORY_S / 60, 0, padding=0)
        self.hist.setMouseEnabled(x=False, y=False)
        self.hist.getAxis("left").setTicks([[(0, ""), (100, "")]])
        self.occ_curve = self.hist.plot(pen=None, fillLevel=0, brush=(102, 187, 106, 70), stepMode="right")
        self.act_curve = self.hist.plot(pen=pg.mkPen("#4fc3f7", width=1))
        grid.addWidget(self.hist, 1, 0)

        grid.addWidget(self._build_panel(), 0, 1, 2, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 3)
        grid.setRowStretch(1, 2)

        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)

        QtGui.QShortcut(QtGui.QKeySequence("Space"), self, lambda: self.pause_btn.toggle())
        QtGui.QShortcut(QtGui.QKeySequence("R"), self, lambda: self.rec_btn.toggle())
        QtGui.QShortcut(QtGui.QKeySequence("E"), self, lambda: self._set_state(False, manual=True))
        QtGui.QShortcut(QtGui.QKeySequence("O"), self, lambda: self._set_state(True, manual=True))
        QtGui.QShortcut(QtGui.QKeySequence("N"), self, self._reset_session)

    def _build_panel(self):
        box = QtWidgets.QFrame()
        box.setStyleSheet("QFrame{background:#161b22;border-radius:8px} QLabel{color:#d0d4d8}")
        box.setMinimumWidth(380)
        lay = QtWidgets.QVBoxLayout(box)
        cap = lambda text: QtWidgets.QLabel(text, styleSheet="font-size:14px;color:#8b949e")
        center = QtCore.Qt.AlignmentFlag.AlignCenter
        self.state_lbl = QtWidgets.QLabel(alignment=center)
        self.since_lbl = QtWidgets.QLabel(alignment=center, styleSheet="font-size:18px")
        self.banner_lbl = QtWidgets.QLabel(alignment=center)
        self.level_bar = QtWidgets.QProgressBar(textVisible=False, maximum=100)
        self.level_bar.setFixedHeight(18)
        self.level_bar.setStyleSheet("QProgressBar{background:#0d1117;border:none;border-radius:4px}"
                                     "QProgressBar::chunk{background:#4fc3f7;border-radius:4px}")
        self.counts_lbl = QtWidgets.QLabel(styleSheet="font-size:16px")
        self.log = QtWidgets.QListWidget()
        self.log.setStyleSheet("QListWidget{background:#0d1117;color:#d0d4d8;font-family:Consolas;"
                               "font-size:14px;border:none}")
        reset_btn = QtWidgets.QPushButton("⟲  Reset for next person")
        reset_btn.setStyleSheet("QPushButton{font-size:20px;font-weight:bold;padding:14px;background:#1565c0;"
                                "color:white;border:none;border-radius:8px}"
                                "QPushButton:pressed{background:#0d47a1}")
        reset_btn.clicked.connect(self._reset_session)
        for w in [cap("Chair"), self.state_lbl, self.since_lbl, self.banner_lbl,
                  cap("Live vibration"), self.level_bar, self.counts_lbl, cap("Event log"), self.log,
                  reset_btn]:
            lay.addWidget(w)
        self._set_banner(None)
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

    # ---- source / controls
    def _start_source(self, port):
        if self.source:
            self.source.stop()
            self.source.join(timeout=1)
        self.stream.reset()
        self.rate_hist.clear()
        self.fs = None
        self._reset_detector()
        for _, _, line in self.markers:
            self.trace.removeItem(line)
        self.markers = []
        if port == SIM:
            self.source = ChairSim(self.stream)
        else:
            self.source = SerialSource(port, self.args.baud, self.stream)
        self.source.start()

    def _reset_detector(self):
        self.done = 0               # samples already run through the detector
        self.env = 0.0
        self.in_event = False
        self.event_peak = 0.0
        self.event_start = 0
        self.quiet_since = -1e9     # sample index when the last event ended

    def _reset_session(self):
        """Back to EMPTY with a clean log, e.g. when a stand-up was missed. Keeps streaming."""
        self.occupied = False
        self.state_since = time.time()
        self.env = 0.0
        self.in_event = False
        self.quiet_since = -1e9
        self.counts = {SIT: 0, STAND: 0}
        for _, _, line in self.markers:
            self.trace.removeItem(line)
        self.markers = []
        self.history.clear()
        self.log.clear()
        self.banner_until = 0.0
        self._set_banner(None)
        self._show_state()
        self._log(f"{time.strftime('%H:%M:%S')}  reset - chair EMPTY", "#8b949e")

    def _set_paused(self, on):
        self.paused = on
        self.pause_btn.setText("Resume" if on else "Pause")

    def _set_recording(self, on):
        if on:
            out = Path("recordings")
            out.mkdir(exist_ok=True)
            path = out / f"chair_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            self.rec_file = open(path, "w", newline="")
            writer = csv.writer(self.rec_file)
            writer.writerow(["host_time", *(self.stream.labels or ["vibration"])])
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

    def _set_state(self, occupied, manual=False, event=None):
        if occupied != self.occupied:
            self.occupied = occupied
            self.state_since = time.time()
        if manual:
            self._log(f"{time.strftime('%H:%M:%S')}  set {'OCCUPIED' if occupied else 'EMPTY'} (manual)",
                      "#8b949e")
        self._show_state()

    def _show_state(self):
        if self.occupied:
            self.state_lbl.setText("OCCUPIED")
            self.state_lbl.setStyleSheet("font-size:52px;font-weight:bold;padding:24px;"
                                         "background:#2e7d32;color:white;border-radius:8px")
        else:
            self.state_lbl.setText("EMPTY")
            self.state_lbl.setStyleSheet("font-size:52px;font-weight:bold;padding:24px;"
                                         "background:#1f2a33;color:#8b949e;border-radius:8px")

    def _set_banner(self, kind):
        if kind is None:
            self.banner_lbl.setText(" ")
            self.banner_lbl.setStyleSheet("font-size:30px;padding:10px")
            return
        arrow = "↓" if kind == SIT else "↑"
        self.banner_lbl.setText(f"{arrow}  {kind}")
        self.banner_lbl.setStyleSheet(f"font-size:30px;font-weight:bold;padding:10px;"
                                      f"color:{EVENT_COLORS[kind]}")

    def _log(self, text, color):
        item = QtWidgets.QListWidgetItem(text)
        item.setForeground(QtGui.QColor(color))
        self.log.insertItem(0, item)

    # ---- detection
    def _estimate_fs(self):
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

    def _vib_column(self, labels):
        return labels.index("vibration") if "vibration" in labels else 0

    def _detect(self, x, first_index, fs):
        """Run new samples through a peak-hold envelope with hysteresis; returns envelope."""
        decay = math.exp(-1.0 / (fs * self.decay.value()))
        thr = self.thr.value()
        lock_n = self.lockout.value() * fs
        env_out = np.empty_like(x)
        env = self.env
        for k, v in enumerate(x):
            idx = first_index + k
            env = max(v, env * decay)
            env_out[k] = env
            if not self.in_event:
                if idx - self.quiet_since < lock_n:
                    if env >= thr * 0.4:
                        self.quiet_since = idx  # still settling after an event: extend lockout
                elif env >= thr:
                    self.in_event, self.event_start, self.event_peak = True, idx, v
                    self._on_event(idx)
            else:
                self.event_peak = max(self.event_peak, v)
                if env < thr * 0.4:
                    self.in_event = False
                    self.quiet_since = idx
                    dur = (idx - self.event_start) / fs
                    item = self.log.item(0)
                    if item is not None:
                        item.setText(item.text() + f"   peak {self.event_peak:.0f}  {dur:.1f} s")
        self.env = env
        return env_out

    def _sit_only(self):
        return self.mode_box.currentData() == "sit"

    def _on_event(self, idx):
        if self._sit_only() and self.occupied:
            kind = MOVE          # already seated: shuffling, or a stand-up we can't trust
        else:
            kind = STAND if self.occupied else SIT
            self.counts[kind] += 1
            self._set_state(not self.occupied, event=kind)
            self._set_banner(kind)
            self.banner_until = time.time() + 4
        self._log(f"{time.strftime('%H:%M:%S')}  {kind:8s}", EVENT_COLORS[kind])
        line = pg.InfiniteLine(angle=90, pen=pg.mkPen(EVENT_COLORS[kind], width=1 if kind == MOVE else 2),
                               label=MARKER_TEXT[kind] or None,
                               labelOpts=dict(position=0.92, color=EVENT_COLORS[kind]))
        self.trace.addItem(line)
        self.markers.append((idx, kind, line))

    # ---- update loop
    def _tick(self):
        self.fs = self._estimate_fs()
        fs = self.fs or NOMINAL_FS
        s = self.stream
        fs_txt = f"{self.fs:,.1f} Hz" if self.fs else "measuring..."
        msg = f"{s.status}   |   fs {fs_txt}   |   samples {s.good:,}   |   bytes {s.bytes:,}"
        if s.bad:
            msg += f"   |   skipped {s.bad:,} (last: {s.last_bad!r})"
        if s.bytes > 2000 and s.good == 0:
            msg += "   |   NO NUMERIC LINES - check baud rate / print format"
        self.status.setText(msg)

        # Detection always runs, even while the display is paused.
        new = s.total - self.done
        if new > 0:
            data, total, labels = s.snapshot(new)
            col = self._vib_column(labels)
            self._detect(data[:, col], total - len(data), fs)
            self.done = total

        now = time.time()
        if now > self.banner_until:
            self._set_banner(None)
        mins, secs = divmod(int(now - self.state_since), 60)
        self.since_lbl.setText(f"for {mins:d}:{secs:02d}")
        if self._sit_only():
            self.counts_lbl.setText(f"sat down {self.counts[SIT]}×    (press N when they leave)")
        else:
            self.counts_lbl.setText(f"sat down {self.counts[SIT]}×    stood up {self.counts[STAND]}×")
        self.level_bar.setValue(int(min(100, self.env)))

        self.history.append((now, self.env, self.occupied))
        while self.history and now - self.history[0][0] > HISTORY_S:
            self.history.popleft()

        if self.paused:
            return
        m = int(fs * TRACE_S)
        data, total, labels = s.snapshot(m)
        if data is None or not len(data):
            return
        x = data[:, self._vib_column(labels)]
        t = (np.arange(len(x)) - len(x)) / fs
        self.raw_curve.setData(t, x)
        self.env_curve.setData(t, self._envelope_for_display(x, fs))

        keep = []
        for idx, kind, line in self.markers:
            pos = (idx - total) / fs
            if pos < -TRACE_S:
                self.trace.removeItem(line)
            else:
                line.setPos(pos)
                keep.append((idx, kind, line))
        self.markers = keep

        h = np.array([(t_ - now, e, o) for t_, e, o in self.history])
        mins_ago = h[:, 0] / 60
        if len(h) > 1:
            self.occ_curve.setData(mins_ago, h[:, 2] * 100)
            self.act_curve.setData(mins_ago, np.minimum(h[:, 1], 100))

    def _envelope_for_display(self, x, fs):
        decay = math.exp(-1.0 / (fs * self.decay.value()))
        out = np.empty_like(x)
        env = 0.0
        for k, v in enumerate(x):
            env = max(v, env * decay)
            out[k] = env
        return out

    def closeEvent(self, ev):
        if self.source:
            self.source.stop()
        self._set_recording(False)
        super().closeEvent(ev)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=SIM, help="COM port, or SIM for synthetic data")
    ap.add_argument("--baud", type=int, default=115200, help="must match Serial.begin()")
    ap.add_argument("--threshold", type=float, default=15, help="vibration score that counts as an event")
    ap.add_argument("--mode", choices=MODES, default="sit",
                    help="sit = detect sit-downs only (reset between people); toggle = sit + stand")
    ap.add_argument("--start-occupied", action="store_true", help="someone is already seated at launch")
    ap.add_argument("--title", default="VPU chair - sit / stand")
    ap.add_argument("--snapshot", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--snapshot-after", type=float, default=15, help=argparse.SUPPRESS)
    args = ap.parse_args()

    pg.setConfigOptions(background="#0d1117", foreground="#d0d4d8")
    app = pg.mkQApp("VPU chair")
    win = ChairDashboard(args)
    win.show()
    if args.snapshot:
        def snap():
            win.grab().save(args.snapshot)
            app.quit()
        QtCore.QTimer.singleShot(int(args.snapshot_after * 1000), snap)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
