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
- Side panel, three tabs: Trial (the form and Record button), Results (analysis of the last trial and of the whole day), Live (event count and log from the band level plot).

### Running trials

1. Trial tab: pick Type "noise only" and press Record, stay still for about 30 s, press Stop. This is the noise baseline for the trials recorded after it.
2. Set Type, Distance and Impacts made (the number of steps, stomps or drops you will make), plus floor, mount and operator once. The Trial ID fills itself in (`fs_1m_03`, numbered after the trials already saved that day); you can edit it.
3. Press Record, make the impacts, press Stop.

Stop saves the same three files as `python -m acquisition.record` (CSV, JSON sidecar with checks, .mat) to `data/raw/<date>/` and switches to the Results tab:

- Trial: pick any trial of the day from the dropdown (newest first) to see its results. Trash... moves the selected trial's .csv, .json and .mat to the Recycle Bin after asking, and removes it from the results; restore it from the Recycle Bin if needed. Trashing the newest trial of a distance frees its number for the next one.
- The selected trial: each impact found, with its time and its level in dB above the noise baseline in 100 to 500 Hz, 500 to 2000 Hz and 2 to 8 kHz. If the count differs from Impacts made, it is shown in orange.
- Summary: one row per type and distance over every trial in that day's folder, including trials recorded with `acquisition.record`: number of trials, impacts found / made, and the mean (standard deviation) level per band.

The analysis is `everyday_sensing.impacts` (the same as `python -m everyday_sensing.impacts data/raw/<date>`). By default each trial is measured against the latest noise-only trial recorded before it. The Baseline dropdown at the top of the Results tab measures every trial against one noise-only trial instead, and the table under it lists each noise-only trial's level per band (median 50 ms RMS in counts), so you can see whether the background changed between them. The trial header shows which baseline was used and its levels. Impacts are found in the 100 to 500 Hz and 500 to 2000 Hz bands; 2 to 8 kHz is shown but not used, because walking on tile gave clicks there with almost nothing below 2 kHz. A weight shift or a step while walking to the mark can still be counted, so check found against made.

### Live view

The live noise baseline is learned from the first 3 s after connecting, so keep still then; press Learn noise (or N) to relearn during a quiet moment. A live event starts when the band level reaches the threshold and ends when it falls 6 dB below it. The default threshold, 10 dB, is also the analysis threshold: in the noise recording of 2026-10-14 the 100 to 500 Hz level stayed within 7.9 dB of its median 99% of the time. Min gap is the shortest quiet time between two events; raise it if one footstep is counted twice. Changing the band relearns the baseline. The threshold set here is also used for the Results tab.

The status bar shows the sample rate, frames, CRC errors, counter gaps and device overflows. Closing the window switches the board back to stats mode.

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
