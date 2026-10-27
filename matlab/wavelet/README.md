# Morlet wavelet transform (MATLAB)

Wavelet transform code from Luis M. Baldelomar Pinto (Prof. Vakakis's lab, LNDVL), sent to the team on 2026-10-27. The three `.m` files are as received.

| File | What it does |
|---|---|
| `freq_inst_morlet.m` | The transform. Takes the FFT of the signal, multiplies it by a Gaussian (the Morlet wavelet in the frequency domain) centered on each analysis frequency, and inverse-FFTs back to time. Returns the magnitude `|W|` as an N x `nf` matrix, and the complex transform if asked for a fourth output. |
| `plotwt.m` | Pads the ends of the signal by mirroring it (to reduce edge artifacts), runs the transform, crops back to the original samples, and plots the scalogram normalized to its own maximum. |
| `Example.m` | Two demos: a sum of sines at 20, 40 and 85 Hz, and a shock (a 1 ms half-sine pulse). |

## Usage

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

## Running it on our trials

Our recordings are 48 kHz, so run it on a window around one event, not a whole trial: a 10 s walking trial at `Fn = 500` needs about 2 GB for the result alone. Event times are in `data/controlled/analysis/<date>/events_<trial>.csv` (`t_s`, from the first sample).

```matlab
addpath('matlab/wavelet')
trial = 'wood_bare_walk_1m_01';
m  = load(fullfile('data/controlled/raw/2026-10-21', [trial '.mat']));
ev = readtable(fullfile('data/controlled/analysis/2026-10-21', ['events_' trial '.csv']));
fs = m.fs_hz;  x = double(m.counts(:));  t = (0:numel(x)-1)'/fs;

ts = ev.t_s(2);                          % second detected step
k  = t >= ts - 0.05 & t <= ts + 0.25;    % 50 ms before to 250 ms after
figure
plotwt(t(k), x(k), 50, 10000, 300, 2, 'e', 'e', 0.2, 0.2, 'sig')
```

## Comparing two signals (carpet against wood)

- `plotwt` divides each plot by its own maximum, so colors in two separate plots cannot be compared. To compare floors, run `freq_inst_morlet` on both and divide by one shared maximum:

  ```matlab
  % tc, xc: a carpet step window and tw, xw: a wood step window, each cut as above
  [~, fr, Mc] = freq_inst_morlet(xc, fs, 50, 10000, 300, 2);   % carpet window
  [~, ~,  Mw] = freq_inst_morlet(xw, fs, 50, 10000, 300, 2);   % wood window
  ref = max([Mc(:); Mw(:)]);
  subplot(211), imagesc(tc, fr, Mc.'/ref), set(gca,'YDir','normal','CLim',[0 1]), title('Carpet')
  subplot(212), imagesc(tw, fr, Mw.'/ref), set(gca,'YDir','normal','CLim',[0 1]), title('Wood')
  colormap(1-pink(256))
  ```

  This skips the mirroring in `plotwt`; with a window that starts and ends in background noise, that only matters near the window edges.

- The magnitude is not the same scale at every frequency: a steady sine of amplitude `A` at `f` shows as `A*sqrt(Fa/(2*f))`. In `Example.m` the 85 Hz sine has twice the amplitude of the 20 Hz one, but the two look about equally strong. Comparing the same frequency between two signals is unaffected. To read amplitudes across frequencies, divide each column by `sqrt(Fa./(2*fr))`.
- Use the same `F0`, `Ff`, `Fn`, `Fa` and window length for both signals.
