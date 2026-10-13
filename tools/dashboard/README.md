# VPU chair dashboard

Live view of the VPU activity score with sit-down / stand-up detection. `vpu_chair_dashboard.py` is the app; `live_dashboard.py` provides the serial line parser, ring buffer, and reconnecting serial reader it uses (and is also a general time series / PSD / spectrogram dashboard on its own).

Flash `firmware/vpu_lab_demo/VPU_Chair_Serial` to the XIAO ESP32-C6 first, and close the Arduino Serial Monitor/Plotter, since only one program can hold the COM port.

Install (from the repo root, ideally in a virtual environment):

```
pip install -r tools/dashboard/requirements.txt
```

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
