# VPU score logger

Logging variant of `firmware/vpu_lab_demo/VPU_Chair_Serial` for the reduced distance sweep (path B). The sensor side is unchanged: 768 kHz clock on D6, data on D7, 5,000 `digitalRead` calls per window, the same startup calibration and the same 0 to 100 score. Only the output differs, so the lab sketch and its dashboard format stay untouched.

Wiring, board settings (USB CDC On Boot: Enabled) and the calibration rule are the same as in [the lab demo README](../vpu_lab_demo/README.md). Keep the sensor still after every reset or power-up, and keep one power cycle per test block: every reboot recalibrates the threshold, which changes the score scale.

## Output

One line per measurement window, tab separated, labelled like the Arduino Serial Plotter:

```
n:1234	t_us:5678901	ones:2513	vibration:0.0	thr:0.0412
```

| Field | Meaning |
|---|---|
| `n` | Window counter, starts at 0 after boot and goes up by 1 every window. A jump on the host means lines were lost; a restart at 0 means the board rebooted. |
| `t_us` | `micros()` at the start of the window. 32-bit, so it wraps after 2^32 us. |
| `ones` | Number of the N reads that saw DATA high (N is printed at startup). |
| `vibration` | The lab score for this window alone (the chair sketch kept the peak since the previous line instead). |
| `thr` | Calibrated noise threshold, in percentage points of duty cycle. Constant until the next reboot. |

Status lines start with `#` and are printed once at startup:

```
# window_us_mean = <measured>
# reads_per_window = 5000
# calibrated, change threshold = <measured>
# fields: n t_us ones vibration thr
```

`window_us_mean` is timed on the board during calibration: the mean time of one calibration iteration, which is one N-read window plus a subtraction. The line rate in normal running also includes printing, so measure it from the `t_us` differences in a recording.

The chair dashboard (`tools/dashboard/vpu_chair_dashboard.py`) still works with this sketch, since it reads the `vibration` field by name and shows the calibrated threshold.

## What `ones` represents

The VPU outputs pulse density modulation: one bit per clock tick, where the fraction of 1s follows the sensor signal (acceleration). `ones / N` is the pulse density averaged over one window, so it moves up and down with the acceleration averaged over that window. Unlike the score, it is linear in the signal, with no log, no threshold subtraction and no clipping at 100.

The window acts as a moving-average (boxcar) lowpass filter. Content much faster than the window length averages out, so the sensor's resonance and most of its specified band are strongly attenuated, and whatever passes the averaging is then sampled once per window with no further filtering, so it aliases. The reads are also not synchronized to the 768 kHz clock, which adds noise to the count. Use `ones` to compare peak deviations between trials within one session, not as an absolute acceleration and not for spectra.

## Measured on hardware

TODO: window duration (`window_us_mean`), line rate from `t_us`, and the count of dropped lines over a long recording. To be filled in from a real board.
