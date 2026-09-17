# Performance Envelope Engine
## Standalone V1 Product Requirements Document

**Working name:** Performance Envelope Engine (PEE)  
**Repository:** Independent repository  
**Relationship to Querysmith:** None required for V1  
**Target:** MongoDB non-production environments  
**Primary purpose:** Determine the measurable operating envelope and breakpoint of a data model + query shape under changing workload conditions.

---

# 1. Product Definition

The Performance Envelope Engine is a standalone benchmarking and modeling system that answers:

> Given a MongoDB data model, indexes, query/access pattern, infrastructure and workload characteristics, under what conditions does the workload stop satisfying its required performance SLO?

The system experimentally evaluates workloads rather than reasoning about them abstractly.

It produces:

- observed performance measurements
- performance response surfaces
- safe/unsafe operating regions
- approximate performance breakpoints
- sensitivity of performance to workload variables
- comparisons between candidate data models

It must operate independently of:

- Querysmith
- MCP
- LLMs
- external query optimization engines
- manually predefined optimization rules

---

# 2. Core Concept

For a specific combination:

```text
Data Model
+
Query Shape
+
Index Configuration
+
Infrastructure
+
Workload
```

estimate:

```text
Performance =
f(
    dataset_size,
    selectivity,
    result_cardinality,
    document_size,
    data_distribution,
    cache_state,
    concurrency,
    query_characteristics
)
```

The system is interested primarily in identifying the boundary:

```text
ACCEPTABLE PERFORMANCE
        ↓
DEGRADATION REGION
        ↓
SLO VIOLATION
```

---

# 3. Primary Product Question

Example:

```text
Model:
Referenced Orders

Query:
Latest 100 orders for customer X

Index:
{customer_id: 1, created_at: -1}

SLO:
p95 < 100 ms
```

The engine should eventually answer:

```text
Observed safe region:

N <= ~100M
selectivity <= ~2%
concurrency <= ~32

Approximate degradation region:

100M–160M documents

First observed SLO violation:

N = 150M
selectivity = 3%
concurrency = 32

Dominant correlated factor:

storage/cache pressure

Confidence:
HIGH
```

---

# 4. V1 Scope

V1 supports:

- MongoDB.
- Read workloads.
- Single query shape per experiment suite.
- Multiple candidate schemas/models.
- Existing or explicitly defined indexes.
- Synthetic datasets.
- User-provided existing datasets.
- Parameterized queries.
- Controlled workload execution.
- Concurrent workload generation.
- Experimental parameter sweeps.
- Performance telemetry collection.
- Regression modeling.
- SLO-based breakpoint detection.
- Change-point detection.
- Model comparison.
- CLI interface.
- Machine-readable reports.
- Human-readable reports.

---

# 5. Non-Goals

V1 will not:

- automatically optimize queries
- automatically recommend indexes
- redesign schemas
- mutate production datasets
- benchmark production environments
- use an LLM to predict latency
- use Querysmith rules
- model write-heavy workloads
- model replication lag
- model Atlas Search
- model vector search
- automatically change MongoDB cluster tiers
- claim universal MongoDB performance limits

All output applies only to the tested environment and workload assumptions.

---

# 6. System Inputs

The engine must be completely self-sufficient.

A test project consists of the following input definitions.

```text
Project
│
├── Environment
├── Data Model
├── Dataset
├── Indexes
├── Query
├── Query Parameters
├── Workload
├── Experiment Dimensions
├── SLO
└── Execution Settings
```

---

# 7. Environment Definition

Defines where the experiment executes.

Example:

```yaml
environment:
  engine: mongodb

  connection:
    uri_env: MONGODB_URI
    database: perf_test

  cluster:
    deployment_type: replica_set
    mongodb_version: auto
    node_count: auto

  safety:
    non_production: true
```

The tool must discover where possible:

```text
MongoDB version
deployment topology
number of shards
replica-set configuration
storage engine
server parameters relevant to testing
```

Environment information becomes part of every result.

---

# 8. Data Model Definition

The schema being tested must be explicitly representable.

Example:

```yaml
model:
  name: referenced_orders

  collections:

    customers:
      fields:
        _id: objectId
        customer_id: integer
        country: string

    orders:
      fields:
        _id: objectId
        customer_id: integer
        created_at: datetime
        status: string
        amount: float
        payload: object
```

The model definition must support:

```text
scalar fields
nested documents
arrays
embedded documents
references
optional fields
```

---

# 9. Index Definition

Indexes are first-class experiment inputs.

Example:

```yaml
indexes:

  orders:

    - name: customer_orders
      keys:
        customer_id: 1
        created_at: -1

    - name: status_idx
      keys:
        status: 1
```

The engine should:

1. inspect currently existing indexes
2. compare them against experiment definition
3. optionally create missing indexes in engine-managed test collections

Never modify indexes on unmanaged datasets unless explicitly enabled.

---

# 10. Dataset Input Modes

Support two modes.

## Mode A — Synthetic Dataset

Generated by the engine.

```yaml
dataset:
  mode: synthetic
```

## Mode B — Existing Dataset

```yaml
dataset:
  mode: existing

  collection: orders
```

Existing dataset mode must be read-only by default.

---

# 11. Synthetic Dataset Definition

Example:

```yaml
dataset:

  mode: synthetic

  seed: 42

  collections:

    orders:

      count: 1000000

      document_size:
        target_bytes: 2048

      fields:

        customer_id:
          type: integer
          cardinality: 100000
          distribution: zipf

        status:
          type: categorical
          values:
            PAID: 0.60
            FAILED: 0.10
            PENDING: 0.15
            SHIPPED: 0.15

        created_at:
          type: datetime
          distribution: uniform

        amount:
          type: float
          min: 1
          max: 10000
```

---

# 12. Supported Data Distributions

V1 should support:

```text
uniform
normal
categorical
sequential
random
zipf
boolean
fixed
```

This is necessary because cardinality and skew materially influence MongoDB performance.

---

# 13. Dataset Scaling

The engine must generate equivalent datasets at different scales.

Example:

```text
100K
1M
10M
50M
```

Scaling must preserve:

```text
field cardinality ratio
distribution characteristics
document size
array size
value skew
relationships between collections
```

Given the same seed and definition, generation should be reproducible.

---

# 14. Query Definition

Queries must be independent inputs.

Example:

```yaml
query:

  id: recent_customer_orders

  operation: find

  collection: orders

  filter:

    customer_id:
      value: "{{customer_id}}"

    created_at:
      $gte: "{{start_date}}"

  projection:
    _id: 1
    amount: 1
    status: 1
    created_at: 1

  sort:
    created_at: -1

  limit: 100
```

V1 should support:

```text
find
aggregate
count
distinct
```

Primary focus remains `find` and simple aggregation pipelines.

---

# 15. Query Parameter Definition

Query literals must not be hard-coded.

Example:

```yaml
parameters:

  customer_id:
    type: dataset_value
    field: customer_id
    strategy: selectivity_targeted

  start_date:
    type: datetime_range
    range:
      - 7d
      - 30d
      - 90d
```

Supported strategies:

```text
random_existing
fixed
selectivity_targeted
hot_value
cold_value
percentile
range
```

---

# 16. Query Shape Canonicalization

Every query must receive a stable shape identifier.

Example canonical form:

```text
find orders

filter:
customer_id = ?
created_at >= ?

sort:
created_at DESC

limit:
100
```

Generate:

```text
SHA256(canonical_query)
```

Literal values must not alter query-shape identity.

---

# 17. Workload Definition

A workload describes how the query is executed.

Example:

```yaml
workload:

  mode: closed_loop

  duration_seconds: 60

  concurrency: 16

  think_time_ms: 0

  timeout_ms: 30000

  connection_pool:
    max_size: 100
```

Support V1 workload types:

```text
fixed_request_count
fixed_duration
```

---

# 18. Primary Experiment Dimensions

V1 should actively support four default variables.

## 18.1 Dataset Size

```text
N
```

Example:

```yaml
documents:
  values:
    - 100000
    - 1000000
    - 10000000
```

## 18.2 Selectivity

```text
matching_documents / collection_documents
```

Example:

```yaml
selectivity:
  values:
    - 0.001
    - 0.01
    - 0.05
```

## 18.3 Concurrency

```yaml
concurrency:
  values:
    - 1
    - 8
    - 32
```

## 18.4 Cache State

```yaml
cache_state:
  values:
    - hot
    - cold
```

Actual cache residency cannot be guaranteed.

Use terminology:

```text
estimated_hot
estimated_cold
```

---

# 19. Optional Experiment Dimensions

The architecture must additionally support:

```text
document size
result cardinality
array cardinality
range width
field cardinality
value skew
payload size
sort size
lookup fan-out
number of shards
```

These need not all receive sophisticated automatic planning in V1.

---

# 20. Fixed Variables

Each experiment must explicitly identify variables being held constant.

Example:

```text
MongoDB version
hardware
storage
index definition
network
query shape
connection configuration
```

This is required to make comparisons interpretable.

---

# 21. Experiment Definition

Example complete experiment:

```yaml
experiment:

  id: customer_order_scaling

  model: referenced_orders.yaml

  dataset: orders_dataset.yaml

  query: recent_customer_orders.yaml

  indexes: indexes.yaml

  dimensions:

    documents:
      values:
        - 100000
        - 1000000
        - 10000000

    selectivity:
      values:
        - 0.001
        - 0.01
        - 0.05

    concurrency:
      values:
        - 1
        - 8
        - 32

    cache_state:
      values:
        - hot
        - cold

  repetitions: 3
```

---

# 22. SLO Definition

Example:

```yaml
slo:

  latency:
    p95_ms: 100
    p99_ms: 250

  error_rate:
    maximum: 0.01
```

The engine must not assume a universal performance threshold.

All GREEN/AMBER/RED classifications derive from user-configured SLOs.

---

# 23. Experiment Planning

Executing the complete Cartesian product may become expensive.

V1 uses:

```text
Coarse Exploration
        ↓
Transition Detection
        ↓
Boundary Refinement
```

---

# 24. Coarse Exploration

Example:

```text
N:
100K
1M
10M

Selectivity:
0.1%
1%
5%

Concurrency:
1
8
32

Cache:
HOT
COLD
```

Total:

```text
54 experiment cells
```

---

# 25. Boundary Refinement

Suppose:

```text
10M documents → GREEN
100M documents → RED
```

Add:

```text
25M
50M
75M
```

Continue until boundary uncertainty reaches:

```text
±15%
```

or configured limit.

---

# 26. Experiment Cell Lifecycle

Each cell executes:

```text
environment validation
        ↓
dataset verification
        ↓
cache conditioning
        ↓
warm-up
        ↓
measurement
        ↓
explain sample
        ↓
telemetry capture
        ↓
result persistence
```

---

# 27. Warm-Up

Default:

```text
20 queries
```

Warm-up observations do not enter benchmark statistics.

For concurrent workloads, perform equivalent warm-up at target concurrency.

---

# 28. Repetitions

Every experiment cell should execute multiple independent repetitions.

Default:

```text
3
```

Report both:

```text
within-run latency distribution
between-run variability
```

---

# 29. Measurement Outputs

Every run must capture:

## Latency

```text
p50
p90
p95
p99
mean
minimum
maximum
```

## Throughput

```text
queries/sec
successful requests
failed requests
timeouts
```

## Query execution

```text
nReturned
totalKeysExamined
totalDocsExamined
executionTimeMillis
```

Derived:

```text
keysExamined / returnedDocument
docsExamined / returnedDocument
```

---

# 30. Environment Metrics

Capture where accessible:

```text
CPU utilisation
WiredTiger cache usage
disk read/write throughput
disk latency
IOPS
connections
memory
network throughput
```

Environment metrics are supporting evidence, not required inputs for the regression model.

---

# 31. Performance Dataset

Every observation becomes a row in the analysis dataset.

Conceptually:

```text
N
selectivity
concurrency
cache_state
document_size
result_count
query_characteristics
cluster_metadata

→

p50
p95
p99
throughput
keysExamined
docsExamined
resource_metrics
```

---

# 32. Performance Modeling

V1 must contain two predictors.

## Model A — Interpretable Baseline

Polynomial / interaction regression.

Purpose:

```text
sanity checking
interpretability
model comparison
```

## Model B — Primary Predictor

Gradient-boosted regression.

Recommended:

```text
XGBoost
```

Target:

```text
log1p(p95_latency_ms)
```

---

# 33. Initial Feature Set

Primary features:

```text
log(dataset_size)
log(selectivity)
log(concurrency)
cache_state
log(document_size)
log(result_cardinality)
```

Later features can be added without modifying the external experiment format.

---

# 34. Why No Neural Network in V1

V1 benchmark datasets will likely be relatively small.

Tree-based models:

```text
require less training data
handle nonlinear relationships
handle feature interactions
are easier to inspect
are easier to debug
```

Neural networks should only be evaluated later if the product accumulates a large cross-workload benchmark corpus.

---

# 35. Why No LLM Predictor

LLMs must not generate latency or breakpoint numbers.

Allowed future LLM responsibilities:

```text
experiment interpretation
natural-language reporting
experiment-plan suggestions
hypothesis generation
```

Numerical predictions must derive from measurements.

---

# 36. Model Validation

Measure:

```text
MAE
RMSE
MAPE
R²
```

Confidence guidance:

```text
MAPE <= 15%
HIGH model confidence

15–30%
MEDIUM

>30%
LOW
```

Never hide model error.

---

# 37. Breakpoint Type 1 — SLO Boundary

Primary breakpoint:

```text
p95 > configured p95 SLO
```

Search response surface for transition:

```text
PASS → FAIL
```

---

# 38. Breakpoint Type 2 — Performance Change Point

A query can degrade dramatically before violating the SLO.

Detect significant changes in slope.

Example:

```text
10M       15 ms
30M       17 ms
60M       22 ms
100M      39 ms
150M     110 ms
```

The engine should flag:

```text
performance inflection approximately 100M–150M
```

---

# 39. V1 Change-Point Algorithm

Use a simple deterministic algorithm initially.

For ordered observations:

```text
slope_i =
(delta latency) /
(delta experiment variable)
```

Flag if:

```text
slope_i >
previous_slope × configured_multiplier
```

Default multiplier:

```text
2
```

More sophisticated algorithms can come later.

---

# 40. Operating Envelope

Map experimental space into:

```text
GREEN
AMBER
RED
```

Default:

```text
GREEN:
p95 <= 70% SLO

AMBER:
70% SLO < p95 <= 100% SLO

RED:
p95 > SLO
```

---

# 41. Sensitivity Analysis

V1 should calculate feature importance from the fitted model.

Example:

```text
Relative performance sensitivity:

Concurrency       41%
Selectivity       31%
Dataset size      19%
Cache condition    9%
```

Treat these as model-level influence metrics, not causal proof.

---

# 42. Deterministic Diagnostic Heuristics

The engine may provide evidence-backed observations.

Example:

```text
docsExamined/nReturned increasing
→ query scan amplification
```

```text
keysExamined/nReturned increasing
→ index scan amplification
```

```text
efficiency ratios stable
latency rises with cold workload
disk reads increase

→ cache/storage sensitivity
```

```text
latency stable at concurrency=1
latency sharply increases at concurrency=32

→ concurrency/resource saturation
```

Every diagnostic should contain:

```text
observation
evidence
confidence
```

Avoid absolute causal claims.

---

# 43. Model Comparison

Multiple complete model definitions may be supplied.

Example:

```text
embedded_model.yaml
referenced_model.yaml
bucketed_model.yaml
```

The engine executes the same logical workload and experiment dimensions for every model.

Output:

```text
                         MODEL A    MODEL B

p95 @ 10M                 20 ms      31 ms
p95 @ 100M                48 ms      91 ms

first SLO violation       310M       120M

safe concurrency           64         24
```

---

# 44. Input Directory Structure

A project should be portable.

Example:

```text
projects/customer-orders/

  project.yaml

  environments/
    local.yaml
    atlas-test.yaml

  models/
    embedded.yaml
    referenced.yaml

  datasets/
    orders.yaml

  indexes/
    embedded.yaml
    referenced.yaml

  queries/
    recent_orders.yaml

  workloads/
    read_workload.yaml

  experiments/
    scale_test.yaml

  slo/
    default.yaml
```

This allows the project to function completely independently of any other repository.

---

# 45. Repository Structure

```text
performance-envelope/

  README.md

  pyproject.toml

  src/perf_envelope/

    cli/

    config/

    environment/
      mongodb.py
      discovery.py
      safety.py

    models/
      schema.py
      indexes.py

    dataset/
      generator.py
      distributions.py
      relationships.py
      scaler.py

    query/
      parser.py
      canonicalizer.py
      parameters.py
      selectivity.py

    workload/
      executor.py
      concurrency.py
      warmup.py

    experiment/
      planner.py
      matrix.py
      refinement.py
      runner.py

    telemetry/
      latency.py
      explain.py
      resources.py

    analysis/
      features.py
      baseline.py
      xgboost_model.py
      validation.py
      sensitivity.py

    frontier/
      slo_boundary.py
      change_point.py
      envelope.py

    diagnostics/
      heuristics.py

    comparison/
      comparator.py

    storage/
      runs.py

    reports/
      json.py
      markdown.py
      html.py

  benchmarks/

  examples/

  tests/
    unit/
    integration/
    synthetic/
    benchmark/
```

---

# 46. Technology Stack

Recommended:

```text
Python 3.12+
PyMongo
Pydantic
Typer
PyYAML
NumPy
Pandas
scikit-learn
XGBoost
SciPy
Rich
Jinja2
pytest
```

Optional later:

```text
FastAPI
Plotly
```

Do not make UI development part of the V1 critical path.

---

# 47. CLI

Required commands:

```bash
perfenv validate
```

Validate all input definitions.

```bash
perfenv inspect
```

Inspect environment/schema/indexes.

```bash
perfenv dataset generate
```

Generate dataset.

```bash
perfenv dataset verify
```

Validate distributions/selectivity.

```bash
perfenv plan
```

Generate experiment matrix without running.

```bash
perfenv run
```

Execute experiment.

```bash
perfenv analyze
```

Fit models and detect boundaries.

```bash
perfenv report
```

Generate output.

```bash
perfenv compare
```

Compare candidate models.

---

# 48. Internal Interfaces

Core classes:

```python
EnvironmentInspector

SchemaDefinition

IndexDefinition

DatasetGenerator

DistributionGenerator

QueryDefinition

QueryParameterGenerator

SelectivityEstimator

WorkloadExecutor

ExperimentPlanner

ExperimentRunner

TelemetryCollector

ExperimentRepository

BaselineRegressor

PerformanceRegressor

ModelValidator

BreakpointDetector

EnvelopeBuilder

SensitivityAnalyzer

ModelComparator

ReportGenerator
```

CLI must contain almost no business logic.

---

# 49. Experiment Result Schema

Example:

```json
{
  "experiment_id": "customer_orders_scale",
  "run_id": "run_001",

  "environment_id": "atlas_test",

  "model_id": "referenced_orders",
  "query_shape_id": "af8271",

  "dataset_size": 10000000,
  "selectivity": 0.01,
  "concurrency": 16,
  "cache_state": "estimated_hot",

  "document_size_bytes": 2048,

  "p50_ms": 21.4,
  "p95_ms": 48.7,
  "p99_ms": 71.1,

  "qps": 331,

  "n_returned": 100,
  "keys_examined": 102,
  "docs_examined": 100,

  "successes": 1000,
  "timeouts": 0,
  "errors": 0
}
```

---

# 50. Result Persistence

Store each experiment locally:

```text
runs/<run_id>/

  configuration/
  environment.json
  observations.parquet
  explain.json
  model/
  analysis.json
  report.json
  report.md
  report.html
```

This makes every experiment self-contained and reproducible.

---

# 51. Built-In Benchmark Suite

Create:

```text
perf-envelope-benchmark-v1
```

It must be fully independent and ship with the repository.

---

# 52. Benchmark A — Indexed Equality

Collection:

```text
users
```

Index:

```text
{user_id: 1}
```

Query:

```text
user_id = X
```

Purpose:

Ideal indexed lookup baseline.

---

# 53. Benchmark B — Equality + Range

Collection:

```text
orders
```

Index:

```text
{customer_id: 1, created_at: -1}
```

Query:

```text
customer_id = X
created_at >= Y
```

Purpose:

Selectivity + scale interaction.

---

# 54. Benchmark C — Filter + Sort

Collection:

```text
events
```

Query:

```text
tenant_id = X
sort timestamp DESC
limit 100
```

Purpose:

Data skew and hot-tenant behavior.

---

# 55. Benchmark D — Multikey

Collection:

```text
products
```

Field:

```text
tags[]
```

Index:

```text
{tags: 1}
```

Vary:

```text
array cardinality
```

Purpose:

Multikey index amplification.

---

# 56. Benchmark E — Embedded vs Referenced

Logical entity:

```text
Customer → Orders
```

Implement:

```text
embedded representation
referenced representation
```

Logical query:

```text
latest 100 orders for customer
```

Purpose:

Demonstrate schema comparison.

---

# 57. Benchmark F — Intentionally Poor Access Pattern

Provide:

```text
unselective query
poor index
high docsExamined ratio
```

Expected result:

Clearly inferior envelope compared with optimized fixture.

This ensures analysis code can distinguish good from bad workloads.

---

# 58. Testing Data Requirements

Tests must not depend entirely on physical performance because CI latency is unstable.

Use two dataset categories.

## Real MongoDB Fixtures

Used for:

```text
dataset generation
indexes
query execution
explain
concurrency
telemetry
```

## Synthetic Observation Fixtures

Used for:

```text
regression validation
boundary detection
change-point detection
sensitivity analysis
reporting
```

---

# 59. Synthetic Observation Test Example

```csv
N,selectivity,concurrency,p95
100000,0.01,1,10
1000000,0.01,1,12
10000000,0.01,1,18
50000000,0.01,1,25
100000000,0.01,1,40
200000000,0.01,1,125
```

With:

```text
SLO = 100 ms
```

Expected:

```text
safe at 100M
unsafe at 200M

boundary between:
100M–200M
```

---

# 60. Unit Testing

Minimum modules:

```text
configuration parsing
schema validation
dataset generation
distribution generation
query canonicalization
parameter generation
selectivity estimation
experiment matrix generation
metric aggregation
feature transformation
regression
boundary detection
change-point detection
envelope classification
```

Target unit coverage:

```text
>= 85%
```

---

# 61. Integration Testing

Use Docker MongoDB.

Integration tests must validate:

```text
create dataset
create indexes
run query
run concurrent query
capture explain
execute experiment matrix
persist observations
fit model
generate report
```

---

# 62. Test Philosophy

Never assert exact timings in CI.

Bad:

```text
assert p95 == 13.4
```

Good:

```text
assert unindexed_docs_examined >
indexed_docs_examined
```

Or:

```text
assert detected_boundary lies within known synthetic range
```

---

# 63. Safety

The tool is potentially destructive because it generates large datasets.

Mandatory protections:

```text
non-production acknowledgement
managed database prefix
managed collection prefix
creation manifest
credential redaction
explicit cleanup command
```

Generated collections:

```text
perfenv_<project>_<collection>
```

The tool must only automatically drop objects created by itself.

---

# 64. Resource Guardrails

Before dataset generation, estimate:

```text
expected storage
expected number of documents
estimated index size
```

Example:

```text
Estimated:
Dataset: 41 GB
Indexes: 13 GB
Total: ~54 GB

Configured safety limit:
60 GB
```

Reject runs exceeding configured limits unless explicitly overridden.

---

# 65. Reproducibility Metadata

Every run stores:

```text
application version
Git commit
MongoDB version
environment metadata
experiment definition
query shape
schema
indexes
dataset seed
dataset distribution
timestamps
```

---

# 66. Output Report

Required outputs:

```text
report.json
report.md
report.html
```

---

# 67. Report Structure

```text
Executive Summary

Environment

Data Model

Indexes

Access Pattern

Dataset Characteristics

Experiment Dimensions

SLO

Measured Performance

Performance Surface

Observed Boundary

Estimated Boundary

Change Points

Sensitivity Analysis

Diagnostics

Confidence

Model Comparison

Limitations

Raw Results
```

---

# 68. Confidence Model

### HIGH

```text
actual SLO crossing observed

and

multiple measurements near boundary

and

model error <= 15%
```

### MEDIUM

```text
boundary primarily interpolated

or

error <= 30%
```

### LOW

```text
significant extrapolation

or

model error > 30%
```

Every breakpoint must be labeled:

```text
OBSERVED

INTERPOLATED

EXTRAPOLATED
```

---

# 69. Required V1 Visualization

Reports should generate at minimum:

```text
p95 vs dataset size

p95 vs concurrency

p95 vs selectivity

GREEN/AMBER/RED 2D envelope

observed vs predicted latency
```

No dashboard is required.

Static HTML plots are sufficient.

---

# 70. Canonical V1 Acceptance Scenario

The repository is considered functional when this full workflow succeeds.

## Model A

Embedded customer-orders.

## Model B

Separate orders collection.

Index:

```text
{customer_id: 1, created_at: -1}
```

## Logical Query

```text
Get latest 100 orders
for customer X
within previous 90 days
```

## Variables

```text
dataset size
selectivity
concurrency
cache condition
```

## SLO

```text
p95 <= 100 ms
```

## Required Output

```text
measured results for both models

performance regression for both models

safe operating regions

first observed SLO violation

estimated breakpoint

change-point detection

relative sensitivity

GREEN/AMBER/RED envelope

comparison summary

confidence
```

---

# 71. Implementation Milestones

## Milestone 1 — Foundations

Build:

```text
configuration system
environment discovery
schema input
index input
query input
validation
```

## Milestone 2 — Data Layer

Build:

```text
synthetic dataset generator
distributions
relationships
scaling
selectivity verification
```

## Milestone 3 — Workload Engine

Build:

```text
parameter generator
MongoDB query executor
concurrency engine
warm-up
metrics collection
explain capture
```

## Milestone 4 — Experiment Engine

Build:

```text
parameter matrix
coarse exploration
experiment persistence
boundary refinement
```

## Milestone 5 — Analysis

Build:

```text
feature processing
baseline regression
XGBoost
validation
sensitivity analysis
```

## Milestone 6 — Frontier

Build:

```text
SLO crossing detection
change-point detection
GREEN/AMBER/RED envelope
confidence calculation
```

## Milestone 7 — Comparison + Reporting

Build:

```text
multi-model comparison
JSON report
Markdown report
HTML report
plots
```

## Milestone 8 — Validation

Build:

```text
benchmark suite
unit tests
integration tests
synthetic observation fixtures
safety controls
README
example projects
```

---

# 72. Future Integration with Querysmith

Querysmith must remain completely optional.

Future integration could look like:

```text
Querysmith
    ↓
exports query definition
    ↓
Performance Envelope Engine
```

Or:

```text
Performance Envelope Engine
    ↓
performance envelope JSON
    ↓
Querysmith
```

Integration should occur through files/API contracts, never through shared internal modules.

The repositories should be deployable, tested and versioned independently.

---

# 73. Future Extensions

Potential future phases:

```text
automatic experiment-space selection

Bayesian optimization

active learning

multi-query workloads

mixed read/write workloads

write scaling

sharding

balancer impact

Atlas telemetry integration

time-series workloads

vector search

Atlas Search

capacity forecasting

growth simulations

hardware/tier comparison

cost/performance frontier

automatic schema comparison

LLM explanation layer
```

---

# 74. Product Boundary

This product is **not a query optimizer**.

It is:

> **A controlled empirical performance-modeling system for determining the operating envelope of a database model and access pattern.**

Querysmith asks:

> How can this query be improved?

This product asks:

> Under what conditions does this model + query stop performing acceptably?

Those are intentionally separate products.

---

# 75. Definition of Done — V1

V1 is complete only when a user can start with:

```text
MongoDB connection
+
schema definition
+
index definition
+
dataset definition
+
query
+
workload
+
SLO
```

and independently obtain:

```text
controlled dataset
+
experiment matrix
+
measured workload results
+
performance model
+
breakpoint
+
operating envelope
+
model comparison
+
reproducible report
```

without installing, importing, accessing or depending upon Querysmith.
