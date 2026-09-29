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
