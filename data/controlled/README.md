# Controlled excitation trials

Floor-vibration trials with a known number of impacts, recorded with `firmware/vpu_pcm_stream` and the PCM dashboard, and analyzed with the det-v1 detection criterion (`everyday_sensing.impacts.DETECTION`; see the repo README).

- `raw/<date>/`: one trial per `<trial>.json` (metadata and recorder checks) and `<trial>.mat` (`counts`, `sample_index`, `host_time_s`, `fs_hz`). The per-sample CSVs the recorder also writes hold the same data and are not included.
- `analysis/<date>/`: `impacts_counts.csv` (made, found, difference, edge events and tags per trial), `events_<trial>.csv` (every detected event, with its tag), `<trial>.analysis.json`, `impacts_trials.csv` and `impacts_summary.csv`. Regenerate with `python -m everyday_sensing.impacts data/controlled/raw/<date>`.
- `figures/<date>/`: one plot per trial and `summary_found_vs_made.png`, from `python scripts/plot_trials.py data/controlled/raw/<date>`.

## 2026-10-19: stomps, bare sensor against vase base enclosure

One walker (Danny) made 10 stomps per trial, standing at 1 m or 2 m from the sensor on the same floor. Three trials per condition:

- `bare_trial_*`: the bare sensor with a weight on top (mount `preloaded`).
- `vase_base_enclosure_*`: the sensor inside the vase base enclosure.
- `fs_noise_01`: the noise-only baseline, recorded before the bare trials. No separate baseline was recorded with the vase enclosure, so the vase trials are also measured against it.

| Condition | 1 m: found / 10 made | 2 m: found / 10 made |
|---|---|---|
| Bare sensor | 10, 10, 10 | 10, 10, 10 |
| Vase base enclosure | 9, 13, 10 | 9, 11, 10 |

In `vase_base_enclosure_2m_02` the first event (0.21 s) is the operator's click on Record, tagged `other_noise`. In `vase_base_enclosure_2m_01` the third stomp peaks at 13.1 dB, just below the 14 dB threshold, so it is missed; the figures show such peaks as hollow circles. Bursts above the threshold that det-v1 merges into a stronger event within 0.25 s are shown as hollow squares. The other extra events in the vase trials are not reviewed yet.

Edits after recording, each noted in the trial's `edit_note`: two trial numbers in the notes corrected, the bare trials renamed from `fs_*` to `bare_trial_*`, the vase 1 m notes corrected from "2 m" to "1 m", and "walking" corrected to "stomping". In this published copy each `.mat` file's `meta` is rewritten to match its `.json` (its samples are unchanged); see `published_note` in each sidecar.
