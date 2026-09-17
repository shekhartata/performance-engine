# Performance Envelope Engine

Standalone benchmarking and modeling system for MongoDB (Atlas) data models and query shapes.

It answers:

> Given a MongoDB data model, indexes, query/access pattern, infrastructure and workload, under what conditions does the workload stop satisfying its required performance SLO?

This is **not** a query optimizer. It measures an operating envelope from experiments.

## Requirements

- Python 3.12+
- A **non-production** MongoDB Atlas cluster
- `MONGODB_URI` set to the Atlas connection string

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then put your Atlas URI in .env
```

## Web UI

Paste a URI, pick a database and collection, drop in a query, run.

```bash
pip install -e ".[dev]"
cd web && npm install && npm run build && cd ..
perfenv ui
```

Open `http://127.0.0.1:8000`. The URI stays in an in-memory session cookie and is never written to run artifacts. Check the non-production box before Run.

During UI development, run the API and Vite together:

```bash
perfenv ui                 # http://127.0.0.1:8000/api
cd web && npm run dev      # http://127.0.0.1:5173  (proxies /api)
```

Advanced (dataset mode, sweeps, SLO, external generator, second model) sits in a drawer. The happy path stays those five steps.

## Quick start

Two ways to point the engine at a target.

### Single-file spec (database + collection + query in one place)

```bash
export MONGODB_URI="mongodb+srv://..."

perfenv validate --spec examples/getting-started/target.yaml
perfenv plan     --spec examples/getting-started/target.yaml
perfenv run      --spec examples/getting-started/target.yaml --ack-non-production --analyze --report
```

See `examples/getting-started/target.yaml`. Override the live target without editing the file:

```bash
perfenv run --spec examples/getting-started/target.yaml \
  --database my_app_db --collection my_collection --ack-non-production
```

### Portable project directory (multi-model / advanced)

```bash
perfenv validate --project examples/getting-started
perfenv inspect  --project examples/getting-started --experiment scale
perfenv plan     --project examples/getting-started --experiment scale

# Destructive / load-generating commands require an explicit ack.
perfenv dataset generate --project examples/getting-started --experiment scale --ack-non-production
perfenv run --project examples/getting-started --experiment scale --ack-non-production --analyze --report
perfenv compare --run <run_id>
```

`--project` and `--spec` are mutually exclusive. The example project uses **small** document counts so it is safe on a shared Atlas test cluster. Scale the `documents` dimension when you want PRD-sized sweeps (100K–10M).

Logical collection names default to `main`. Query files can omit `collection`; the engine resolves it from the dataset (or from `--collection` / `target:`).

## Benchmarking data you already have

Give it a database and a collection and it points the whole suite there:

```bash
perfenv run -p examples/getting-started -e scale \
  --database my_app_db --collection my_collection --ack-non-production --analyze --report
```

Or declare it once for the project in `project.yaml`:

```yaml
target:
  uri_env: MONGODB_URI
  database: my_app_db
  collection: my_collection
```

Or in the experiment file (see `examples/getting-started/experiments/existing.yaml`):

```yaml
experiment:
  id: existing
  database: my_app_db
  collection: my_collection
  model: referenced
  query: primary
```

`--database` / `--collection` also work on `validate`, `inspect` and `plan`, and override whatever the YAML says.

What changes in this mode:

- The collection is treated as **read-only**: no generation, no drops, no index creation. `dataset generate` and `dataset verify` refuse to run.
- The **dataset-size sweep is disabled**, because you cannot resize data you did not create. `documents` is recorded as the collection's actual count and the boundary-refinement rounds are skipped.
- Selectivity, concurrency and cache state are still swept normally, and latency, explain stats, envelope and diagnostics all still apply.
- Only one candidate model runs, since a single physical collection makes model comparison meaningless.

Scaling and multi-model comparison remain synthetic-only.

## External generator + per-scale curves

If a separate repo materializes representative data at 10K / 50K / 100K / 500K, point the engine at it with `data.mode: external`. The engine shells out once per scale, then measures the same query shape against `collection_{documents}` (one collection per scale) and plots a curve per N.

```yaml
data:
  mode: external
  collection_template: "{collection}_{documents}"
  generator:
    command: "python generate.py --count {documents} --uri {uri} --db {database} --collection {collection} --seed {seed}"
    working_dir: ../my-data-generator
    timeout_seconds: 1800
    reuse_if_present: true
    count_tolerance: 0.05
```

```bash
perfenv run --spec examples/getting-started/external.yaml \
  --ack-non-production --allow-external-writes --analyze --report
```

`--allow-external-writes` is required because generated collections sit outside the `perfenv_` prefix. `reuse_if_present` skips the generator when the templated collection already has a matching count.

Unmeasured scales listed under `sweep.project_documents` are extrapolated from the fitted baseline and tagged `extrapolated: true` in the report, so they are never mistaken for measured points.

## What gets measured

Experiment **dimensions** (X): dataset size, selectivity, concurrency, cache state (`estimated_hot` / `estimated_cold`).

Performance **outcomes** (Y): latency percentiles (p50–p99), throughput, errors/timeouts, explain stats (`keysExamined`, `docsExamined`).

The regression target is `log1p(p95_latency_ms)`. GREEN / AMBER / RED regions are derived from the user SLO (`p95 <= 70%` / `<= 100%` / `>`).

## CLI

| Command | Purpose |
|---|---|
| `perfenv validate` | Validate YAML inputs |
| `perfenv inspect` | Discover Atlas topology / indexes |
| `perfenv dataset generate` | Generate synthetic data into `perfenv_*` collections |
| `perfenv dataset verify` | Check counts / cardinality |
| `perfenv plan` | Print the coarse experiment matrix |
| `perfenv run` | Execute cells, refine SLO boundaries, persist a run |
| `perfenv analyze` | Fit baseline + XGBoost, detect breakpoints |
| `perfenv report` | Write `report.json` / `report.md` / `report.html` |
| `perfenv compare` | Compare candidate models |
| `perfenv ui` | Local web UI (URI → collection → query → run) |
| `perfenv cleanup` | Drop engine-managed (and recorded external) collections |

## Safety

- Requires `safety.non_production: true`
- Load-generating commands require `--ack-non-production`
- Synthetic collections are named `perfenv_<project>_<collection>`
- External generator writes require `--allow-external-writes`
- Storage estimates are checked against `max_storage_gb` before synthetic generation
- Only objects recorded in the creation manifest are dropped by `cleanup`

## Tests

```bash
pytest tests/unit tests/synthetic
# Atlas integration (skipped unless MONGODB_URI is set)
pytest tests/integration
```

On macOS, XGBoost needs `libomp` (`brew install libomp`). If the native library cannot load, analysis automatically falls back to scikit-learn gradient boosting.

## Layout

See `src/perf_envelope/` for the package map from the V1 PRD. Example project: `examples/getting-started/`. Built-in shapes: `benchmarks/`.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
