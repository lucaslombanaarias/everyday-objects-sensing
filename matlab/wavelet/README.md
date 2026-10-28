# Morlet wavelet transform (MATLAB)

Wavelet transform code from Luis M. Baldelomar Pinto (Prof. Vakakis's lab, LNDVL), sent to the team on 2026-10-27. `Example.m`, `plotwt.m` and `freq_inst_morlet.m` are as received; `compare_floors_wavelet.m` is ours. Everything here was run in MATLAB R2025a on 2026-10-28.

| File | What it does |
|---|---|
| `freq_inst_morlet.m` | The transform. Takes the FFT of the signal, multiplies it by a Gaussian (the Morlet wavelet in the frequency domain) centered on each analysis frequency, and inverse-FFTs back to time. Returns the magnitude `|W|` as an N x `nf` matrix, and the complex transform if asked for a fourth output. |
| `plotwt.m` | Pads the ends of the signal by mirroring it (to reduce edge artifacts), runs the transform, crops back to the original samples, and plots the scalogram normalized to its own maximum. |
| `Example.m` | Two demos: a sum of sines at 20, 40 and 85 Hz, and a shock (a 1 ms half-sine pulse). |
| `compare_floors_wavelet.m` | Runs the transform on single steps from several of our trials and plots them on one shared dB scale, with a table of peak levels. Use this one to compare floors. |

## Comparing floors

From the repo root:

```matlab
addpath('matlab/wavelet')
compare_floors_wavelet()     % events 2 to 4 of carpet_bare_walk_1m_01 against wood_bare_walk_1m_01
compare_floors_wavelet(["carpet_bare_walk_2m_01" "wood_bare_walk_2m_01"], 1:5)
R = compare_floors_wavelet(SaveAs="steps.png");   % also save the figure and keep the table
```

Each row of the figure is a trial and each column an event. Events are det-v1 event numbers from `data/controlled/analysis/<date>/events_<trial>.csv`; some events are not steps (`walking_steps_review.csv` lists them), so check the event times before choosing. Options: `Date`, `Pre` and `Post` (window in s around the event, default 0.10 and 0.25), `Pad`, `Fmin`, `Fmax`, `Nf`, `Fa` (default 50 Hz, 10 kHz, 300, 2), `RangeDb` (default 40) and `SaveAs`. `help compare_floors_wavelet` has the details.

The table gives, per event, the strongest point (`peak_db`, `peak_ms`, `peak_hz`) and the strongest point at or above 1 kHz (`peak_above_1khz_*`). Levels are dB relative to the strongest point in any panel, and times are ms from the event time.

Why it does not just call `plotwt` on each step:

- **Shared scale.** `plotwt` divides each plot by its own maximum, so colors in two plots cannot be compared. This divides every panel by one shared maximum.
- **dB, not linear.** Most steps have their strongest point below about 120 Hz (5 of the 6 in the default run). On a linear scale that peak sets the maximum and the content above 1 kHz comes out nearly white.
- **Amplitude correction.** `|W|` for a steady sine of amplitude `A` at `f` is `A*sqrt(Fa/(2*f))`, so low frequencies read stronger than they are (in `Example.m` the 85 Hz sine has twice the amplitude of the 20 Hz one, but its band peaks at about the same level). This divides by `sqrt(Fa/(2*f))` so a sine of amplitude `A` reads `A` at every frequency.
- **Window starts early.** det-v1 marks a step after it starts: in the default run the burst starts 10 to 50 ms before the event time. So the window starts 100 ms before it.
- **Real padding.** It transforms 100 ms of real signal on each side of the window and then crops it, instead of mirroring.
- **Memory.** Our recordings are 48 kHz, so it works on one step at a time. A whole 10 s walking trial at 500 frequencies needs about 2 GB for the result alone.

Default run (bare sensor, 1 m, 2026-10-21):

| Event | Carpet: peak above 1 kHz (dB) | Wood: peak above 1 kHz (dB) |
|---|---|---|
| 2 | -28.8 | -17.5 |
| 3 | -35.5 | -8.5 |
| 4 | -22.4 | -21.7 |

On carpet the content above 1 kHz is over within about 40 ms of the event time. On wood it is stronger in events 2 and 3, reaches higher (up to about 10 kHz in event 2), and has later bursts 60 to 100 ms after the event time. Event 4's peak above 1 kHz is about the same on both floors, and carpet event 3 is weak at all frequencies, so the difference varies a lot from step to step: compare more steps and trials before drawing a conclusion.

## Using the lab's functions directly

```matlab
plotwt(t, x, F0, Ff, Fn, Fa, miri, mirf, chp1, chp2, Label)
```

| Argument | Meaning |
|---|---|
| `t`, `x` | Time (s) and signal, uniformly sampled |
| `F0`, `Ff` | Lowest and highest frequency to analyze (Hz). `F0` must be above 0 |
| `Fn` | Number of frequencies, spaced linearly from `F0` to `Ff` |
| `Fa` | Morlet center frequency: about how many cycles the wavelet spans. 2 to 6 is typical |
| `miri`, `mirf` | Padding at the start and end: `'o'` odd mirror (flipped about the end sample, keeps value and slope continuous), `'e'` even mirror (plain reflection), `''` none |
| `chp1`, `chp2` | Fraction of the signal length used for the start and end padding |
| `Label` | `'IMF'` squares the plotted magnitude (energy). Anything else plots the magnitude |

`Fa` sets the time-frequency trade-off. At frequency `f` the wavelet's time spread is about `Fa/f` seconds and its bandwidth about `f/(2*pi*Fa)` Hz, so a smaller `Fa` (the shock demo uses 2) localizes impacts better in time, and a larger one separates nearby frequencies better. A shock shows as a vertical stripe that smears out in time toward low frequencies.

For a signal that starts right at an impact (as in the shock demo), use no padding at the start: mirroring would copy the impact into negative time.

A quick look at one step with `plotwt` (linear scale, normalized to its own maximum):

```matlab
addpath('matlab/wavelet')
trial = 'wood_bare_walk_1m_01';
m  = load(fullfile('data/controlled/raw/2026-10-21', [trial '.mat']));
ev = readtable(fullfile('data/controlled/analysis/2026-10-21', ['events_' trial '.csv']));
fs = m.fs_hz;  x = double(m.counts(:));  t = (0:numel(x)-1)'/fs;

ts = ev.t_s(2);                          % second detected step
k  = t >= ts - 0.10 & t <= ts + 0.25;    % 100 ms before to 250 ms after
figure
plotwt(t(k), x(k), 50, 10000, 300, 2, 'e', 'e', 0.2, 0.2, 'sig')
```

`Example.m` draws its signals in black (`'k-'`), so they do not show on MATLAB's dark theme; switch MATLAB to the light theme to see them.
