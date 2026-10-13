# VPU dashboards

Two live viewers for serial sensor data, both from the lab:

- `vpu_chair_dashboard.py`: the VPU activity score with sit-down / stand-up detection, for the chair demo.
- `live_dashboard.py`: the general lab viewer (time series, PSD, spectrogram, peak-frequency readout). It also provides the serial line parser, ring buffer, and reconnecting serial reader that the chair dashboard uses.

Close the Arduino Serial Monitor/Plotter before running either, since only one program can hold the COM port. Commands are run from the repo root.

Install (ideally in a virtual environment):

```
pip install -r tools/dashboard/requirements.txt
```

## Chair dashboard

Flash `firmware/vpu_lab_demo/VPU_Chair_Serial` to the XIAO ESP32-C6 first.

List serial ports to find the board:

```
python -m serial.tools.list_ports -v
```

Run against the board:

```
python tools/dashboard/vpu_chair_dashboard.py --port COM5
```

Run with the synthetic chair, no hardware needed:

```
python tools/dashboard/vpu_chair_dashboard.py --port SIM
```

The status bar shows the measured line rate (`fs`, about 200 Hz from the firmware), the number of parsed samples, and how many lines were skipped. Recordings started with the Record button (or R) are written to `recordings/`, which is not committed.

## Lab viewer

List serial ports:

```
python tools/dashboard/live_dashboard.py --list
```

Run against a board with the VPU preset:

```
python tools/dashboard/live_dashboard.py --preset vpu --port COM5
```

The VPU preset is meant for a raw audio-rate waveform (the lab used 921600 baud, `--baud 921600`). None of the sketches in `firmware/vpu_lab_demo` stream one: they send a thresholded 0 to 100 activity score at about 200 lines/s, so with them the time series is usable but the PSD and spectrogram are not meaningful.

Run with a synthetic VPU signal (2 kHz, voiced bursts and chewing thumps), no hardware needed:

```
python tools/dashboard/live_dashboard.py --preset vpu --port SIM
```
