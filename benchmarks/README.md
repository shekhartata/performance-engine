# Built-in benchmark suite (perf-envelope-benchmark-v1)

Each subdirectory is a portable project. They are intentionally small so they can run against a shared Atlas test cluster. Every benchmark uses the generic layout: `models/model.yaml`, `datasets/dataset.yaml`, `indexes/indexes.yaml`, `queries/query.yaml`, all targeting the default logical collection `main`.

| ID | Shape | Purpose |
|---|---|---|
| A | Indexed equality | Ideal lookup baseline |
| B | Equality + range | Selectivity × scale |
| C | Filter + sort | Skew / hot-tenant |
| D | Multikey | Array cardinality amplification |
| E | Embedded vs referenced | Schema comparison (also `examples/getting-started`) |
| F | Intentionally poor access | Unselective / missing index |

Run:

```bash
perfenv run --project benchmarks/a_indexed_equality --experiment scale --ack-non-production --analyze --report
```
