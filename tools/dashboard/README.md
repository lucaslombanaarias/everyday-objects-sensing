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

- Waveform: the last 3 s, high-passed at 100 Hz by default (the 41 Hz hum measured at rest otherwise dominates the view), with the live RMS level in the chosen band, the live threshold and a dot on each live event.
- Level + spectrogram (checkbox, off by default): the live level in dB above the noise baseline, and a spectrogram of the last 10 s, 0 to 8 kHz.
- Side panel, three tabs: Trial (the form and Record button), Results (det-v1 analysis of one trial and of the whole day), Live (event count and log of the live preview).

### Running trials

1. Trial tab: pick Excitation "noise only" and the Mount, press Record, stay still for about 30 s, press Stop. This is the noise baseline for the trials recorded after it, and becomes the form's Baseline noise.
2. Set Excitation (`drop_stand`, `ball_drop`, `single_step`, `double_step`, `walking`, `stomp`), Impacts made, Excitation point, Sensor radius (impact point to sensor center), Mount (`tape`, `putty_wax`, `loose`, `preloaded`), and if relevant Drop height and Tip, plus floor and operator once. The Trial ID fills itself in (`fs_1m_03`, numbered after the trials already saved that day); you can edit it.
3. Press Record, make the impacts, press Stop.

Record refuses to start without an ID prefix, an excitation, impacts made (1 or more) and a mount, and says what is missing; nothing is recorded until the form is complete. Notes carry over from one trial to the next, so when they are unchanged from the last saved trial Record also stops once and outlines them in orange: edit them, or press Record again to keep them. Once a recording has started it is always saved at Stop, even if the form was changed meanwhile (the result line then says what is incomplete).

Stop saves the same three files as `python -m acquisition.record` (CSV, JSON sidecar with checks, .mat) to `data/raw/<date>/`, runs the analysis on that day's folder, writes its outputs to `data/analysis/<date>/` and switches to the Results tab:

- The detection criterion, stated at the top (det-v1, see the repo README). The live band, threshold and min gap do not change it.
- Trial: pick any trial of the day from the dropdown (newest first). Trash... moves the selected trial's .csv, .json and .mat to the Recycle Bin after asking, and removes it from the results; restore it from the Recycle Bin if needed.
- Made against found and the difference (orange if not zero), how many events are at the edges (within 1 s of the start or end), and the tags so far. The baseline used is shown, in orange with the recorded one if the trial records a different block baseline.
- One plot: the RMS over the 5 ms detection window (bold orange) against the threshold line, each event numbered (orange if at an edge), and the high-passed waveform drawn faint behind it. The detector tests the RMS, not the waveform: on the 18 October evening stomps a waveform peak was 1.4 to 3.2 times its 5 ms RMS, so the waveform can cross the line without an event. Peaks of the RMS between 10 dB and the threshold are shown as hollow circles with their level, and listed under made / found, so a miss shows how close it came. Separate bursts above the threshold that det-v1 merged into a stronger event within 0.25 s are shown as hollow squares. "Show detection level (dB) and spectrogram" adds those two plots below it.
- Events: time from the start, clock time, dB above the baseline, the edge flag, and a tag for each (`true_impact`, `walk_in_out`, `other_noise`, `unknown`). A tag is saved to `events_<trial>.csv` as soon as it is picked, and kept when the folder is analyzed again.
- Summary: one row per excitation and radius over every trial in that day's folder, including trials recorded with `acquisition.record`: trials, found / made, the difference, and the mean (standard deviation) detection dB per event.

What a trial looks like:

![The PCM dashboard showing one stomp trial](../../docs/figures/dashboard_trial_example.png)

The PCM dashboard on `tile_bare_stomp_2m_01` (saved as `bare_trial_2m_01`, the name in the screenshot; 10 stomps at 2 m, bare sensor, 18 October evening session): the full window, then the whole Results tab, which scrolls in the window. No board was attached when this was taken, so the live view on the left shows the synthetic SIM stream. The summary table at the bottom groups trials by excitation and radius only, so the bare-sensor and vase trials of that session are pooled there.

`data/raw/` and `data/analysis/` are not committed. To share a day of trials with the other computers, run `python scripts/share_trials.py` from the repo root: it copies the newest day's trials (.json and .mat) and analysis to `data/controlled/`, commits and pushes, so `git pull` brings them elsewhere. Give a date (`python scripts/share_trials.py 2026-10-21`) for another day, or the path of the folder if the trials are somewhere else (`python scripts/share_trials.py "C:\Users\me\Desktop\2026-10-21"`). It prints the folder it looked in.

By default each trial is measured against the latest noise-only trial recorded before it. The Baseline dropdown at the top measures every trial against one noise-only trial instead; those outputs (and their tags) go to a `baseline_<trial>/` subfolder. The table under it lists each noise-only trial's median detection level in counts, so you can see whether the background changed between them, and its false alarms: det-v1 events in the noise-only trial measured against its own baseline, with the rate per minute (orange above 3 per minute, the limit in the floor procedure). Pick a noise-only trial in the Trial dropdown to see its false alarms on the plot and tag their causes. If an events CSV is open in Excel the outputs cannot be written; the folder line at the bottom says so.

### Live view

The live view is a preview for watching the floor, not the det-v1 criterion: it uses a hysteresis detector whose baseline is learned from the first 3 s after connecting, so keep still then; press Learn noise (or N) to relearn during a quiet moment. A live event starts when the level reaches the live threshold and ends when it falls 6 dB below it. The default band is the det-v1 band and detection window, and the default threshold (14 dB) and Min gap (0.25 s) match det-v1. Changing the band relearns the baseline.

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
