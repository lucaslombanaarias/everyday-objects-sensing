# Controlled excitation trials

Floor-vibration trials with a known number of impacts, recorded with `firmware/vpu_pcm_stream` and the PCM dashboard, and analyzed with the det-v1 detection criterion (`everyday_sensing.impacts.DETECTION`; see the repo README).

- `raw/<date>/`: one trial per `<trial>.json` (metadata and recorder checks) and `<trial>.mat` (`counts`, `sample_index`, `host_time_s`, `fs_hz`). The per-sample CSVs the recorder also writes hold the same data and are not included.
- `analysis/<date>/`: `impacts_counts.csv` (made, found, difference, edge events and tags per trial), `events_<trial>.csv` (every detected event, with its tag), `<trial>.analysis.json`, `impacts_trials.csv` and `impacts_summary.csv`. Regenerate with `python -m everyday_sensing.impacts data/controlled/raw/<date>`.
- `figures/<date>/`: one plot per trial and `summary_found_vs_made.png`, from `python scripts/plot_trials.py data/controlled/raw/<date>`.

## 2026-10-14: noise-only baselines

The three noise-only recordings from the first footstep session (tile, sensor taped down), published so the false-alarm rates quoted for det-v1 can be checked. Nothing was done during them, so every event is a false alarm. The two published rates measure them differently:

| Trial | Length | Against its det-v1 baseline, over the whole recording | Against its quietest 2 s, over the recording after its first 1 s |
|---|---|---|---|
| `fs_noise_01` | 30.0 s | 1 event, 2.0 per minute | 2 events, 4.1 per minute |
| `fs_noise_02` | 66.6 s | 2 events, 1.8 per minute | 2 events, 1.8 per minute |
| `fs_noise_03` | 47.0 s | 1 event, 1.3 per minute | 1 event, 1.3 per minute |

The first column (1.3 to 2.0) is what the dashboard, `impacts_counts.csv` and the floor procedure's 3 per minute limit use: the baseline is the median 5 ms RMS of the trial, skipping its first 1 s, which is what det-v1 gives a noise-only trial. The second (1.3 to 4.1) is how the rate was first measured on 10/14, and is the figure in the `Detection.threshold_db` docstring and `firmware/vpu_pcm_stream/README.md`. To reproduce both, in Python from the repo root:

```python
from pathlib import Path
from everyday_sensing import impacts as I
for p in sorted(Path("data/controlled/raw/2026-10-14").glob("*.json")):
    _, x, fs = I.load_trial(p)
    s = x.size / fs
    a = len(I.find_impacts(x, fs, I.noise_baseline(x, fs))["impacts"])
    b = len(I.find_impacts(x, fs)["impacts"])  # no baseline given: the quietest 2 s
    print(p.stem, f"{a / s * 60:.1f}", f"{b / (s - 1) * 60:.1f}")
```

`fs_noise_02` contains one footstep-sized event at 31 s, most likely someone moving. Two of the sidecars had notes carried over from a walking trial; they described the walk, not the noise recording, and are removed in this copy (see `published_note`).

## Session of 18 October, evening: stomps, bare sensor against vase base enclosure

Folder `session_2026-10-18_evening`. The session started on the evening of 18 October; these trials ran just after midnight, 00:15 to 00:30 on 19 October, so each trial keeps its actual `date` (2026-10-19) and `start_time`.

One walker (Danny) made 10 stomps per trial, standing at 1 m or 2 m from the sensor on the same floor. Three trials per condition:

- `bare_trial_*`: the bare sensor with a weight on top (mount `preloaded`).
- `vase_base_enclosure_*`: the sensor inside the vase base enclosure.
- `fs_noise_01`: the noise-only baseline, recorded before the bare trials. No separate baseline was recorded with the vase enclosure, so the vase trials are also measured against it.

| Condition | 1 m: found / 10 made | 2 m: found / 10 made |
|---|---|---|
| Bare sensor | 10, 10, 10 | 10, 10, 10 |
| Vase base enclosure | 9, 13, 10 | 9, 11, 10 |

In `vase_base_enclosure_2m_02` the first event (0.21 s) is the operator's click on Record, tagged `other_noise`. In `vase_base_enclosure_2m_01` the third stomp peaks at 13.1 dB, just below the 14 dB threshold, so it is missed; the figures show such peaks as hollow circles. Bursts above the threshold that det-v1 merges into a stronger event within 0.25 s are shown as hollow squares.

The two 1 m vase trials were reviewed against the stomp rhythm (about 0.6 s between stomps, as in `vase_base_enclosure_1m_03`, which is regular from first to last stomp); there is no video of them:

- `vase_base_enclosure_1m_02` (10 made, 13 found): events 7, 9, 11 and 13 come 0.27 to 0.30 s after stomps 7 to 10 (events 6, 8, 10 and 12) and are weaker. Each is a second burst after a stomp that lands just past det-v1's 0.25 s merge window, so the stomp was counted twice; they are tagged `other_noise`. A fifth such burst, 0.27 s after event 4, stayed below the threshold (13.9 dB at 6.04 s). One stomp, at about 5.2 s where the rhythm puts it, is missed (peaks of 11.4 and 12.4 dB at 5.17 and 5.34 s). Event 5 (6.53 s, 14.2 dB) is off the rhythm and tagged `unknown`: it is either stomp 6 or the second burst of a stomp at 6.36 s that peaked at 13.3 dB. So the 13 events are 9 stomps (8 if event 5 is a burst), 4 double counts, and that one ambiguous event, with 1 or 2 stomps missed. What makes the second burst (the enclosure rocking or rebounding, or the foot) is not known; the bare-sensor trials do not show it.
- `vase_base_enclosure_1m_01` (10 made, 9 found): between events 4 and 5 there is a gap of 1.83 s, three stomp intervals, so two stomps are missed (near misses of 11.4 and 10.1 dB at 5.75 and 6.35 s). Event 9 (9.48 s) comes 0.70 s after event 8, off the rhythm, and is tagged `unknown`. So the 9 events are 8 stomps and 1 unexplained event, with 2 stomps missed.

Neither result changes det-v1: a longer merge window would absorb the double counts, but that is a question for a later detector version, tested on new recordings. The other vase trials (`1m_03`, `2m_01`, `2m_03`) and the bare-sensor trials are not reviewed event by event beyond what is noted above.

Edits after recording, each noted in the trial's `edit_note`: two trial numbers in the notes corrected, the bare trials renamed from `fs_*` to `bare_trial_*`, the vase 1 m notes corrected from "2 m" to "1 m", and "walking" corrected to "stomping". In this published copy each `.mat` file's `meta` is rewritten to match its `.json` (its samples are unchanged); see `published_note` in each sidecar.

## 2026-10-21: bare against enclosed, carpet and wood

Folder `2026-10-21`, 18:09 to 19:38. Trial IDs are `<floor>_<bare|enclosed>_<excitation>_<radius>m_<nn>`, and noise baselines `<floor>_<bare|enclosed>_noise_01`. `walk` is Elsa walking in place, 10 steps; `drop4in` and `drop7in` are the drop stand at 4 in (10.2 cm) and 7 in (17.8 cm), flat tip, one drop per trial. The bare sensor has a weight on top (mount `preloaded`); the enclosed trials are recorded with the same mount. Three trials per radius, recorded in this order:

| Block | Noise baseline | Trials (found / made per trial) |
|---|---|---|
| Carpet, bare | `carpet_bare_noise_01`, 0 false alarms per minute | `drop7in` 1 m only: 3, 5, 3 / 1. `walk` 1 m: 10, 10, 10; 2 m: 10, 10, 9; 3 m: 9, 7, 8 / 10. `drop4in` 1 m: 2, 1, 1; 2 m: 1, 1, 3; 3 m: 1, 2, 2 / 1 |
| Wood, bare | `wood_bare_noise_01`, 4.1 false alarms per minute | `walk` 1 m: 10, 13, 10; 2 m: 10, 9, 11; 3 m: 12, 10, 10 / 10. `drop4in` 1 m (four trials): 2, 3, 2, 2; 2 m: 3, 2, 2; 3 m: 3, 2, 2 / 1 |
| Wood, enclosed | none before it; measured against `wood_bare_noise_01` | `walk` 1 m: 11, 11, 10; 2 m: 10, 10, 10; 3 m: 10, 10, 10 / 10. `drop4in` 1 m: 4, 3, 3; 2 m: 2, 2, 2; 3 m: 3, 2, 2 / 1 |
| Carpet, enclosed | `wood_enclosed_noise_01` (recorded on wood, after the enclosed wood drops) | `drop4in` 1 m: 1, 1, 1; 2 m: 1, 1, 1; 3 m: 1, 2, 1 / 1. No walking. |

Not reviewed event by event, and no events are tagged yet. Most drops on wood give 2 or 3 events for one drop. The `wood_bare_noise_01` false-alarm rate is above the 3 per minute limit in the floor procedure. Twenty of the drop trials, all 2 to 4.6 s long, fail only the recorder's sample-rate check (measured 48,970 to 49,740 samples/s against the host clock over too short a recording); none has lost samples, CRC errors, overflows or clipping.

Edits after recording, each noted in the trial's `edit_note` with the original ID and notes: every trial renamed to the scheme above; the empty notes filled in from it; the three 7 in drops (first saved as `noise_1m_01` to `_03`) corrected from `ball_drop` at 7.0 cm to `drop_stand` at 17.8 cm and numbered in recording order (their notes had said trials 1, 3, 2); `carpet_bare_walk_1m_01` corrected from 1 impact made to 10; the first enclosed walk at 1 m (saved as `wood_1m_05`) and the fourth bare drop at 1 m on wood (`wood_1m_04`) and second drop at 2 m on carpet (`carpet_2m_04`) given their block's names. In this published copy each `.mat` file's `meta` is rewritten to match its `.json` (its samples are unchanged).
