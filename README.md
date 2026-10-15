# everyday-objects-sensing

AI Design Catalog prototype (live site): https://lucaslombanaarias.github.io/everyday-objects-sensing/

## everyday_sensing (Python package): Development

These instructions are only for the `everyday_sensing` Python package in `src/everyday_sensing/`, not the rest of the repository.

Install (from the repo root, ideally in a virtual environment):

```
pip install -r requirements.txt
```

Run lint and tests:

```
ruff check .
pytest -q
```

Regenerate the decimation filter report in `results/decimation_filter/`:

```
python -m everyday_sensing.filter_report
```

Regenerate the slide figures in `docs/figures/`:

```
python scripts/plot_filter_figures.py
```

## Recording data

Flash `firmware/vpu_pcm_stream` to the XIAO ESP32-C6 (see [its README](firmware/vpu_pcm_stream/README.md)), close the Arduino Serial Monitor and any dashboard, then record one trial per command from the repo root:

```
python -m acquisition.record --port COM3 --trial-id <id> --duration <s> --distance-m <m> --excitation-type drop --mass-g <g> --drop-height-cm <cm> --tip <tip> --mount-method <method> --floor-material <material> --operator <initials> --fw-commit <commit>
```

Ambient baseline with no excitation:

```
python -m acquisition.record --port COM3 --trial-id d1_noise_01 --duration 30 --noise-only
```

Each trial writes three files to `data/raw/<date>/` (not committed):

- `<trial-id>.csv`: `sample_index` (device counter, so lost samples show as jumps), `host_time_s` (arrival time of the sample's frame), `counts` (int16 PCM; PDM full scale is +-32768).
- `<trial-id>.json`: all metadata flags, the repo and firmware commits, and the sanity checks.
- `<trial-id>.mat`: the same signal, `fs_hz`, and `meta` and `checks` structs for MATLAB.

The checks are printed after every trial: CRC errors, counter gaps and missing samples, device DMA overflows, clipped samples, and the sample rate measured against the host clock. The command exits non-zero if any of them fail. `python -m acquisition.record --help` lists all metadata flags.

## Decimation filter figures

Recordings are captured at 48 kHz and decimated by 32 to 1,500 Hz through a linear-phase Kaiser FIR (passband edge 500 Hz, ripple <= 0.1 dB, attenuation >= 96 dB from 750 Hz). All numbers on the figures are computed from the designed taps.

Magnitude response of the filter against the spec, with the sensor's datasheet resonance band (4 to 5 kHz) shaded:

![Decimation filter magnitude response](docs/figures/filter_response.png)

A synthetic 4.2 kHz tone decimated naively (top) folds to 300 Hz, inside the kept band; through the filter and `decimate()` (bottom) it is suppressed:

![Aliasing of a 4.2 kHz tone with and without the filter](docs/figures/aliasing_demo.png)
