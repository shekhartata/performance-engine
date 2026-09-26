# Performance Envelope Engine

Measures where a MongoDB (Atlas) query stops meeting its latency SLO.

> Given a data model, indexes, a query or access pattern, and a workload, under what conditions does p95 go over the SLO?

This is **not** a query optimizer. It runs experiments and draws the operating envelope from what it measured.

## Setup

You need Python 3.12+, Node.js (for the web UI), and a **non-production** Atlas cluster.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # put your Atlas URI in MONGODB_URI

cd web && npm install && npm run build && cd ..
```

On macOS, XGBoost needs `libomp` (`brew install libomp`). If it cannot load, analysis falls back to scikit-learn.

## Three ways to run a test

| | Guided (UI) | Spec YAML (UI) | Spec file (CLI) |
|---|---|---|---|
| Best for | One query, quick check | Anything the CLI can do, without a terminal | Repeatable runs, scripts, CI |
| You provide | Database, collection, query, a few settings | A full spec, pasted | A spec file path |
| Connection | Session URI | Session URI (a `uri` in the paste is ignored) | `MONGODB_URI` from `.env` |

All three produce the same kind of run and the same reports. Guided mode is a form that builds a spec for you. Spec mode hands the engine the whole document.

### 1. Guided (UI)

```bash
perfenv ui                    # http://127.0.0.1:8000
```

1. Paste the Atlas URI and connect.
2. Stay on **Guided**. Pick a database and collection.
3. Paste a query as JSON, using placeholders for values that should vary:

   ```json
   { "filter": { "status": "{{status}}" }, "sort": { "created_at": -1 }, "limit": 50 }
   ```

4. Tick the non-production box and press **Run test**.

**Advanced** holds the dataset mode, sweep values, SLO, duration, a second query to compare, and the built-in union plans.

The URI stays in an in-memory session and is never written to run artifacts.

### 2. Spec YAML (UI)

Same page, **Spec YAML** tab. Paste a complete spec, or press **Insert union example** to load `examples/union_fanout.yaml`. Tick the non-production box and run.

Use this when the form cannot express what you want: extra sweep settings, multi-step request plans, data recipes, or threshold goals.

### 3. Spec file (CLI)

```bash
perfenv validate --spec examples/getting-started/target.yaml
perfenv plan     --spec examples/getting-started/target.yaml
perfenv run      --spec examples/getting-started/target.yaml --ack-non-production --analyze --report
```

Point a spec at other data without editing it:

```bash
perfenv run --spec examples/getting-started/target.yaml \
  --database my_app_db --collection my_collection --ack-non-production
```

## Writing a spec

The smallest useful spec is a target, a query, a sweep, and an SLO:

```yaml
name: my-envelope
target:
  database: my_app_db
  collection: orders
query:
  operation: find
  filter:
    status: "{{status}}"
  limit: 50
  parameters:
    status:
      type: dataset_value
      field: status
      strategy: random_existing
sweep:
  concurrency: [1, 4, 8]
  cache_state: [hot, cold]
slo:
  p95_ms: 100
safety:
  non_production: true
```

Each combination of sweep values is one test cell. `{{placeholders}}` are filled per request.

For patterns the form does not cover, a spec can also use these sections:

| Section | What it does |
|---|---|
| `sweep.<any_name>` | Add your own sweep axis, for example `union_count: { values: [1, 2, 4, 8], bind: shape }` |
| `plans` | Replace the single query with steps: `find`, `aggregate`, `count`, `distinct`. Steps can run in parallel and be combined with `concat` or `merge_sort`. `$for` repeats a step over a slice such as `collections[0:union_count]` |
| `data.scales` | Map a document count to the database that holds that size |
| `data.recipe` | Have the engine build the test collections (currently `clone_embed`) and reuse them on later runs |
| `goal.find_threshold` | Report the largest value of an axis that still meets the SLO, for each group |
| `workload`, `execution` | Duration, warmup, timeouts, repetitions, refinement rounds |

`examples/union_fanout.yaml` uses all of them: server-side `$unionWith` against application fan-out across N collections.

Two limits to know. A sweep axis only changes the run if something reads it: the built-in axes (`documents`, `concurrency`, `cache_state`, `selectivity`, `matches_per_key`) or a `{{placeholder}}` inside a plan. Steps also cannot pass results to one another, so there are no app-side joins, writes, or transactions.

## Using data you already have

With `data.mode: existing`, the engine only reads the target collection. It does not generate, drop, or index anything. The document-count sweep is replaced by the collection's actual size unless `data.scales` maps sizes to databases. Selectivity, concurrency, and cache state are still swept.

If a separate tool generates data at each size, use `data.mode: external`. See `examples/getting-started/external.yaml` and pass `--allow-external-writes`.

## What you get

Each run writes a folder under `runs/<run_id>/`:

- `report.html`, `report.md`, `report.json`: the envelope (GREEN ≤ 70% of SLO, AMBER ≤ 100%, RED over), latency curves, thresholds, and the main drivers of latency
- `configuration/run_settings.json`: every setting used, marked as set by you, a default, or derived (for example, connection pool size and its formula)
- `observations.csv`: one row per measurement

UI runs also show results on the page, with links to the reports.

## Safety

- Every spec needs `safety.non_production: true`, and every run needs `--ack-non-production` (or the checkbox in the UI).
- Collections the engine creates start with `perfenv_`. `perfenv cleanup` drops only what the engine recorded creating.
- Writes by an external generator require `--allow-external-writes`.
- Connection strings are redacted in every artifact.

## CLI reference

| Command | Purpose |
|---|---|
| `perfenv ui` | Local web UI |
| `perfenv validate` | Check a spec or project |
| `perfenv inspect` | Show Atlas topology and indexes |
| `perfenv plan` | Print the test cells without running them |
| `perfenv run` | Run the test cells and save the run |
| `perfenv analyze` | Fit models and find the SLO boundary |
| `perfenv report` | Write the HTML, Markdown, and JSON reports |
| `perfenv compare` | Compare candidate models in a run |
| `perfenv dataset generate` | Generate synthetic data |
| `perfenv cleanup` | Drop engine-created collections |

A project directory (`--project examples/getting-started`) splits the spec into files for multi-model comparison. `--project` and `--spec` cannot be used together.

## Development

```bash
pytest tests/unit tests/synthetic
pytest tests/integration         # needs MONGODB_URI

perfenv ui                       # API on :8000
cd web && npm run dev            # UI on :5173, proxies /api
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
