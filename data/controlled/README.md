# Controlled excitation trials

Floor-vibration trials, and on 2026-11-04 toilet trials, with a known number of impacts, recorded with `firmware/vpu_pcm_stream` and the PCM dashboard, and analyzed with the det-v1 detection criterion (`everyday_sensing.impacts.DETECTION`; see the repo README).

- `raw/<date>/`: one trial per `<trial>.json` (metadata and recorder checks) and `<trial>.mat` (`counts`, `sample_index`, `host_time_s`, `fs_hz`). The per-sample CSVs the recorder also writes hold the same data and are not included.
- `analysis/<date>/`: `impacts_counts.csv` (made, found, difference, edge events and tags per trial), `events_<trial>.csv` (every detected event, with its tag), `<trial>.analysis.json`, `impacts_trials.csv` and `impacts_summary.csv`. Regenerate with `python -m everyday_sensing.impacts data/controlled/raw/<date>`.
- `figures/<date>/`: one plot per trial and `summary_found_vs_made.png`, from `python scripts/plot_trials.py data/controlled/raw/<date>`.

## 2026-10-14: noise-only baselines

The three noise-only recordings from the first footstep session (tile, sensor taped down), published so the false-alarm rates quoted for det-v1 can be checked. Nothing was done during them, so every event is a false alarm. The two published rates measure them differently:

| Trial | Length | Against its det-v1 baseline, over the whole recording | Against its quietest 2 s, over the recording after its first 1 s |
|---|---|---|---|
| `tile_bare_noise_01` | 30.0 s | 1 event, 2.0 per minute | 2 events, 4.1 per minute |
| `tile_bare_noise_02` | 66.6 s | 2 events, 1.8 per minute | 2 events, 1.8 per minute |
| `tile_bare_noise_03` | 47.0 s | 1 event, 1.3 per minute | 1 event, 1.3 per minute |

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

`tile_bare_noise_02` contains one footstep-sized event at 31 s, most likely someone moving. Two of the sidecars had notes carried over from a walking trial; they described the walk, not the noise recording, and are removed in this copy (see `published_note`). First saved as `fs_noise_01` to `_03`; renamed to `tile_bare_noise_01` to `_03` on 2026-10-22 (see `edit_note`).

## Session of 18 October, evening: stomps, bare sensor against vase base enclosure

Folder `session_2026-10-18_evening`, tile floor. The session started on the evening of 18 October; these trials ran just after midnight, 00:15 to 00:30 on 19 October, so each trial keeps its actual `date` (2026-10-19) and `start_time`.

One walker (Danny) made 10 stomps per trial, standing at 1 m or 2 m from the sensor on the same floor. Three trials per condition:

- `tile_bare_stomp_*`: the bare sensor with a weight on top (mount `preloaded`).
- `tile_enclosed_stomp_*`: the sensor inside the vase base enclosure.
- `tile_bare_noise_01`: the noise-only baseline, recorded before the bare trials. No separate baseline was recorded with the vase enclosure, so the vase trials are also measured against it.

| Condition | 1 m: found / 10 made | 2 m: found / 10 made |
|---|---|---|
| Bare sensor | 10, 10, 10 | 10, 10, 10 |
| Vase base enclosure | 9, 13, 10 | 9, 11, 10 |

In `tile_enclosed_stomp_2m_02` the first event (0.21 s) is the operator's click on Record, tagged `other_noise`. In `tile_enclosed_stomp_2m_01` the third stomp peaks at 13.1 dB, just below the 14 dB threshold, so it is missed; the figures show such peaks as hollow circles. Bursts above the threshold that det-v1 merges into a stronger event within 0.25 s are shown as hollow squares.

The two 1 m vase trials were reviewed against the stomp rhythm (about 0.6 s between stomps, as in `tile_enclosed_stomp_1m_03`, which is regular from first to last stomp); there is no video of them:

- `tile_enclosed_stomp_1m_02` (10 made, 13 found): events 7, 9, 11 and 13 come 0.27 to 0.30 s after stomps 7 to 10 (events 6, 8, 10 and 12) and are weaker. Each is a second burst after a stomp that lands just past det-v1's 0.25 s merge window, so the stomp was counted twice; they are tagged `other_noise`. A fifth such burst, 0.27 s after event 4, stayed below the threshold (13.9 dB at 6.04 s). One stomp, at about 5.2 s where the rhythm puts it, is missed (peaks of 11.4 and 12.4 dB at 5.17 and 5.34 s). Event 5 (6.53 s, 14.2 dB) is off the rhythm and tagged `unknown`: it is either stomp 6 or the second burst of a stomp at 6.36 s that peaked at 13.3 dB. So the 13 events are 9 stomps (8 if event 5 is a burst), 4 double counts, and that one ambiguous event, with 1 or 2 stomps missed. What makes the second burst (the enclosure rocking or rebounding, or the foot) is not known; the bare-sensor trials do not show it.
- `tile_enclosed_stomp_1m_01` (10 made, 9 found): between events 4 and 5 there is a gap of 1.83 s, three stomp intervals, so two stomps are missed (near misses of 11.4 and 10.1 dB at 5.75 and 6.35 s). Event 9 (9.48 s) comes 0.70 s after event 8, off the rhythm, and is tagged `unknown`. So the 9 events are 8 stomps and 1 unexplained event, with 2 stomps missed.

Neither result changes det-v1: a longer merge window would absorb the double counts, but that is a question for a later detector version, tested on new recordings. The other vase trials (`1m_03`, `2m_01`, `2m_03`) and the bare-sensor trials are not reviewed event by event beyond what is noted above.

Edits after recording, each noted in the trial's `edit_note`: two trial numbers in the notes corrected, the bare trials renamed from `fs_*` to `bare_trial_*`, the vase 1 m notes corrected from "2 m" to "1 m", and "walking" corrected to "stomping". In this published copy each `.mat` file's `meta` is rewritten to match its `.json` (its samples are unchanged); see `published_note` in each sidecar. On 2026-10-22 every trial was renamed to `tile_<bare|enclosed>_stomp_<radius>m_<nn>` (first `bare_trial_*` and `vase_base_enclosure_*`) and the noise baseline to `tile_bare_noise_01` (first `fs_noise_01`), the floor set to tile, and the stomp notes put in one format; the tags moved with them.

## 2026-10-21: bare against enclosed, carpet and wood

Folder `2026-10-21`, 18:09 to 19:38, and the enclosed walks on carpet at 20:54 to 21:00. Trial IDs are `<floor>_<bare|enclosed>_<excitation>_<radius>m_<nn>`, and noise baselines `<floor>_<bare|enclosed>_noise_01`. `walk` is Elsa walking in place, 10 steps; `drop4in` and `drop7in` are the drop stand at 4 in (10.2 cm) and 7 in (17.8 cm), flat tip, one drop per trial. The 7 in height is not part of the plan: these three trials are the only ones. The bare sensor has a weight on top (mount `preloaded`); the enclosed trials are recorded with the same mount. Three trials per radius, recorded in this order:

| Block | Noise baseline | Trials (found / made per trial) |
|---|---|---|
| Carpet, bare | `carpet_bare_noise_01`, 0 false alarms per minute | `drop7in` 1 m only: 3, 5, 3 / 1. `walk` 1 m: 10, 10, 10; 2 m: 10, 10, 9; 3 m: 9, 7, 8 / 10. `drop4in` 1 m: 2, 1, 1; 2 m: 1, 1, 3; 3 m: 1, 2, 2 / 1 |
| Wood, bare | `wood_bare_noise_01`, 4.1 false alarms per minute | `walk` 1 m: 10, 13, 10; 2 m: 10, 9, 11; 3 m: 12, 10, 10 / 10. `drop4in` 1 m (four trials): 2, 3, 2, 2; 2 m: 3, 2, 2; 3 m: 3, 2, 2 / 1 |
| Wood, enclosed | none before it; measured against `wood_bare_noise_01` | `walk` 1 m: 11, 11, 10; 2 m: 10, 10, 10; 3 m: 10, 10, 10 / 10. `drop4in` 1 m: 4, 3, 3; 2 m: 2, 2, 2; 3 m: 3, 2, 2 / 1 |
| Carpet, enclosed | `wood_enclosed_noise_01` (recorded on wood, after the enclosed wood drops) | `drop4in` 1 m: 1, 1, 1; 2 m: 1, 1, 1; 3 m: 1, 2, 1 / 1 |
| Carpet, enclosed, later | `carpet_enclosed_noise_01` (64 s), 0.9 false alarms per minute | `walk` 1 m: 10, 10, 10; 2 m: 9, 9, 9; 3 m: 7, 7, 7 / 10 |

The drops are not reviewed event by event, and no events are tagged yet. Most drops on wood give 2 or 3 events for one drop. The `wood_bare_noise_01` false-alarm rate is above the 3 per minute limit in the floor procedure. Twenty of the drop trials, all 2 to 4.6 s long, fail only the recorder's sample-rate check (measured 48,970 to 49,740 samples/s against the host clock over too short a recording); none has lost samples, CRC errors, overflows or clipping.

The 36 walking trials were reviewed by hand against their step rhythm (steps about 0.5 s apart): `analysis/2026-10-21/walking_steps_review.csv` gives, per trial, the 10 steps planned, what det-v1 found, the steps that show up in the signal, the time and level of each step det-v1 missed, and the time and cause of each event that is not a step. The 20 trials with 10 found have 10 evenly spaced steps. In the other 16, det-v1 missed 21 steps, all visible in the waveform and on the rhythm but under its 14 dB threshold (6.3 to 14.0 dB, 13 of them 11 to 14 dB; all but one on carpet at 2 or 3 m), and counted 7 events that are not steps (a burst at the start of a recording, an impact before walking began, the click on Stop, and narrow spikes). With them, every reviewed trial shows its 10 steps except: `carpet_bare_walk_3m_03` has only 9 steps in the signal, `wood_enclosed_walk_1m_01` has 11 evenly spaced steps (11 were taken, not the 10 recorded as made), and `wood_bare_walk_3m_01` is left undecided (12 step-like impacts, steady until 5.7 s, then gaps of 0.9 to 1.3 s). The review changes no det-v1 output and tags no events; `python scripts/plot_floor_slides.py --walking` plots it.

Edits after recording, each noted in the trial's `edit_note` with the original ID and notes: every trial renamed to the scheme above; the empty notes filled in from it; the three 7 in drops (first saved as `noise_1m_01` to `_03`) corrected from `ball_drop` at 7.0 cm to `drop_stand` at 17.8 cm and numbered in recording order (their notes had said trials 1, 3, 2); `carpet_bare_walk_1m_01` corrected from 1 impact made to 10; the first enclosed walk at 1 m (saved as `wood_1m_05`) and the fourth bare drop at 1 m on wood (`wood_1m_04`) and second drop at 2 m on carpet (`carpet_2m_04`) given their block's names; the later carpet noise baseline and walks (saved as `noise_noise_02` and `carpet_enclosed_<r>m_<nn>`) renamed and their notes filled in. In this published copy each `.mat` file's `meta` is rewritten to match its `.json` (its samples are unchanged).

## 2026-11-04: toilet, ice drops and stream trials at three sensor positions

Folder `2026-11-04`, 18:36 to 19:18. The sensor is taped (mount `tape`) to a toilet at one of three positions, in this order: `base`, the base of the toilet under the seat; `behindseat`, between the seat lid and the tank; `tanklid`, on top of the tank lid. Trial IDs are `toilet_<position>_<ice|pour>_<nn>`, numbered in recording order. `ice` (excitation `ice_drop`) is 5 ice cubes dropped from seat height into the center of the bowl water; they are fridge ice-maker cubes, flat on one side and rounded on the other (`tip`). `pour` (excitation `water_pour`) is a stream trial: one cup of water poured slowly from seat height into the bowl. A pour is not a number of impacts, so its impacts made is null. Neither the drop height in cm nor the distance from the bowl to the sensor was measured (`drop_height_cm` and `distance_m` are null); `floor_material` is `toilet` and `floor_location` gives the sensor position. At each position the order is ice 1, stream 1, ice 2, stream 2, ice 3, stream 3:

| Position | Recorded | Noise baseline | Ice: found / 5 made | Stream: found |
|---|---|---|---|---|
| Base | 18:36 to 18:50 | `toilet_base_noise_01`, 0 false alarms in 21.5 s | 14, 19, 29 | 27, 30, 23 |
| Behind seat | 19:04 to 19:10 | none; measured against `toilet_base_noise_01` | 13, 12, 6 | 22, 21, 15 |
| Tank lid | 19:13 to 19:18 | none; measured against `toilet_base_noise_01` | 9, 7, 8 | 16, 11, 14 |

Every ice trial has its 5 drops, 2.0 to 3.7 s apart, and det-v1 finds all 45 (the weakest at 26.9 dB). Each drop is tagged `true_impact` at its first hit; every other event in the ice trials is tagged `other_noise`: the second hit or splash 0.3 to 0.7 s after a drop, clicks and knocks between drops, activity at Record and Stop, and in the later base trials the hiss described below. In `toilet_tanklid_ice_03` the first two drops are 0.67 s apart (1.30 and 1.97 s) because a cube slipped; both are tagged. In `toilet_tanklid_ice_01` the fourth drop gives two hits of similar size 0.32 s apart (10.00 and 10.32 s); the first is tagged. The stream trials are not reviewed: det-v1 marks the peaks of a continuous splash, 11 to 30 per pour, which do not count anything.

How a cube enters the water (flat side down, upright edge first, or rounded side down) changes how hard it hits, and which way each cube landed was not recorded: within one trial the 5 drops differ by 12 to 28 dB. Over the 15 drops at each position, in dB above the base noise baseline, the base has a median of 59.5 dB (middle half 51.7 to 68.5), behind the seat 50.5 dB (45.1 to 54.1) and the tank lid 38.5 dB (33.4 to 45.2), so compare positions on these medians rather than on single drops. The median level over each pour is 25.0 to 26.9 dB at the base, 17.2 to 18.6 dB behind the seat and 12.5 to 16.7 dB on the tank lid. The loudest drop at the base (`toilet_base_ice_02`, 1.15 s) reaches 32,251 counts, 0.14 dB below full scale; no sample is clipped.

Only one noise baseline was recorded, at the base before the base trials, so the behind-seat and tank-lid trials are measured against it. The quietest 2 s of each tank lid trial is 1.0 to 2.2 dB below it, so the tank lid levels read about 1.5 dB low. Measured instead against each position's own background (the median detection level between events in its trials, more than 0.5 s from any event and from the pour: base +1.5 dB, behind the seat +0.6 dB, tank lid -1.6 dB against the base noise recording), the median drop is 58.0, 49.9 and 40.1 dB and the median pour 24.3, 16.9 and 14.3 dB, which is what `toilet_levels.png` shows. The order of the positions is the same either way: it is in the signal itself, a median drop of 2,206, 787 and 195 counts (5 ms RMS above 100 Hz). During the base trials a hiss above 2 kHz builds up, from a source nobody noticed: the median 2-8 kHz level (over each ice trial, and outside the pour in each stream trial) goes from 0 dB in the first ice trial to 2.7, 5.9, 6.0 and 9.1 dB, and 14.8 dB in the last stream trial. The bands below 2 kHz stay at the noise level, and the behind-seat and tank-lid trials do not have it (-3.1 to +1.0 dB). It makes most of the 24 `other_noise` events in `toilet_base_ice_03` and hides no drop. The last base pour is also shorter than the other two (4.2 s above its own background, against 8.7 and 8.4 s).

Edits after recording, each noted in the trial's `edit_note` with the original ID, the excitation, distance and impacts made as recorded, and the original notes. The trials were saved as `toilet_base_01`, `toilet_base_0m_<nn>`, `toilet_behind_seat_<nn>`, `toilet_behind_seat_1m_<nn>` and `toilet_on_top_1m_<nn>`, and the baseline as `toilet_noise_01`; every ice and stream trial had 5 impacts made, excitation `ball_drop` (base) or `drop_stand` (the others) and a distance of 0 or 1 m; only the notes said which were ice and which were stream trials (the notes called them pee trials). Every trial was renamed to the scheme above; its excitation, impacts made, distance, floor, impact point and notes were set from the operator's description and the cube shape added; and the second stream trial behind the seat and on the tank lid, both noted as trial 3, was numbered 2. The base had a fourth stream trial: the first one (saved as `toilet_base_0m_01` at 18:44:46, 13.4 s), about 10 dB quieter than the others and redone 2.5 minutes later, is removed, and the other three are numbered 1 to 3. Two trials are cropped, each with the span kept, the reason and the original checks under `cropped`: `toilet_base_noise_01` to its first 21.5 s of 25.0 s (two impacts at 21.95 and 22.56 s, after a smaller one at 21.79 s), and `toilet_behindseat_pour_03` to 1.00 to 7.65 s of 8.96 s (knocks at 0.41, 7.84 and 8.91 s, most likely the cup on the seat), with its `start_time` moved to its first kept sample. The uncropped originals are not published: they stay in `data/raw/2026-11-04/uncropped/` on the recording laptop, and the removed stream trial in that laptop's Recycle Bin. On 2026-11-05 "pee trial" in the stream trials' notes was renamed "stream trial". Each `.mat` file's `meta` is rewritten to match its `.json` (its samples are unchanged).

Figures: one plot per trial and `summary_found_vs_made.png` in `figures/2026-11-04/` (`python scripts/plot_trials.py data/controlled/raw/2026-11-04`). Slide figures in `docs/figures/`, each for the ice drops alone (`toilet_ice_*.png`), the stream trials alone (`toilet_stream_*.png`) and both side by side (`toilet_*.png`): `levels` (every drop's and pour's level by position, against the position's own background), `traces` (the second trial at each position, as the detector sees it) and `spectra` (average spectra by position) from `python scripts/plot_toilet_slide_figures.py`, and `wavelet` (Morlet scalograms of the median drop and of a stream trial at each position, on one dB scale per figure) from `wavelet_toilet()` in `matlab/wavelet/`. In the scalograms the base's background before the pour is a steady band at 4 to 5 kHz, the sensor's datasheet resonance: the hiss above is there.
