# VPU dashboards

Live viewers for the VPU:

- `vpu_pcm_dashboard.py`: the real waveform from `firmware/vpu_pcm_stream`, with spectrogram, event (footstep) detection and recording. Use this one with the PCM firmware.
- `vpu_chair_dashboard.py` (from the lab): the VPU activity score with sit-down / stand-up detection, for the chair demo.
- `live_dashboard.py` (from the lab): the general lab viewer (time series, PSD, spectrogram, peak-frequency readout). It also provides the serial line parser, ring buffer, and reconnecting serial reader that the chair dashboard uses.

Each one needs the matching firmware: the PCM dashboard reads binary frames and the lab dashboards read text lines, so neither works with the other's firmware.

Close the Arduino Serial Monitor/Plotter before running any of them, since only one program can hold the COM port. Commands are run from the repo root.

Install (ideally in a virtual environment). The PCM dashboard also uses the repo's Python package, which the root requirements install:

```
pip install -r requirements.txt
```

## PCM dashboard

Flash `firmware/vpu_pcm_stream` first (see its README), then:

```
python tools/dashboard/vpu_pcm_dashboard.py --port COM3
```

Run with synthetic footsteps, no hardware needed:

```
python tools/dashboard/vpu_pcm_dashboard.py --port SIM
```

- Waveform: the last 3 s, high-passed at 100 Hz by default (the 41 Hz hum measured at rest otherwise dominates the view).
- Band level: the 50 ms RMS in the chosen band, in dB above the noise baseline, with the threshold line and a dot on each detected event. The default band, 100 to 500 Hz, is the band the analysis keeps after `preprocess.decimate`.
- Spectrogram: the last 10 s, 0 to 8 kHz (the sensor's specified band).
- Side panel: event count and log (level above baseline and duration of each), noise baseline, and recording.

The noise baseline is learned from the first 3 s after connecting, so keep still then; press Learn noise (or N) to relearn during a quiet moment. An event starts when the band level reaches the threshold (default 20 dB, the same 10x criterion used for the stomp check) and ends when it falls 6 dB below it. Min gap is the shortest quiet time between two events; raise it if one footstep is counted twice. Changing the band relearns the baseline.

Record (or R) saves the same three files as `python -m acquisition.record` (CSV, JSON sidecar with checks, .mat) to `data/raw/<date>/` when stopped, with the trial id and notes typed in the panel. The status bar shows the sample rate, frames, CRC errors, counter gaps and device overflows. Closing the window switches the board back to stats mode.

Keys: Space pause display, R record, N learn noise, C clear events.

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
