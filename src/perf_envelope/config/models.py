"""Pydantic configuration models for all V1 project inputs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Default logical collection name used when a project does not care about naming.
DEFAULT_COLLECTION = "main"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ConnectionConfig(FrozenModel):
    uri_env: str = "MONGODB_URI"
    uri: str | None = None
    database: str = "perf_test"


class ClusterConfig(FrozenModel):
    deployment_type: str = "auto"
    mongodb_version: str = "auto"
    node_count: str | int = "auto"


class SafetyConfig(FrozenModel):
    non_production: bool = True
    managed_prefix: str = "perfenv"
    max_storage_gb: float = 60.0
    allow_index_mutation_on_unmanaged: bool = False
    allow_override_storage_limit: bool = False


class EnvironmentConfig(FrozenModel):
    engine: Literal["mongodb"] = "mongodb"
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)


class FieldSpec(FrozenModel):
    type: str = "string"
    cardinality: int | None = None
    distribution: str = "uniform"
    values: dict[str, float] | list[Any] | None = None
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    std: float | None = None
    alpha: float = 1.2
    p: float = 0.5
    value: Any = None
    prefix: str = "val"
    start: str | None = None
    end: str | None = None
    references: str | None = None
    optional: bool = False
    optional_p: float = 0.1
    fields: dict[str, "FieldSpec | str"] | None = None
    items: "FieldSpec | str | None" = None
    array_size: int | tuple[int, int] | None = None
    target_bytes: int | None = None

    @field_validator("distribution")
    @classmethod
    def _known_distribution(cls, value: str) -> str:
        allowed = {
            "uniform",
            "normal",
            "categorical",
            "sequential",
            "random",
            "zipf",
            "boolean",
            "fixed",
        }
        if value not in allowed:
            raise ValueError(f"Unsupported distribution '{value}'. Allowed: {sorted(allowed)}")
        return value


class CollectionModel(FrozenModel):
    fields: dict[str, FieldSpec | str] = Field(default_factory=dict)


class ModelConfig(FrozenModel):
    name: str
    collections: dict[str, CollectionModel]

    @model_validator(mode="before")
    @classmethod
    def _single_collection_shorthand(cls, data: Any) -> Any:
        """Allow `model: {name: x, fields: {...}}` without naming a collection."""
        if isinstance(data, dict) and "collections" not in data and "fields" in data:
            data = dict(data)
            fields = data.pop("fields")
            data["collections"] = {DEFAULT_COLLECTION: {"fields": fields}}
        return data


class IndexSpec(FrozenModel):
    name: str | None = None
    keys: dict[str, int]
    unique: bool = False


class IndexConfig(FrozenModel):
    indexes: dict[str, list[IndexSpec]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _unwrap(cls, data: Any) -> Any:
        # A bare list of index specs applies to the default collection.
        if isinstance(data, list):
            return {"indexes": {DEFAULT_COLLECTION: data}}
        if isinstance(data, dict) and "indexes" in data and set(data.keys()) == {"indexes"}:
            inner = data["indexes"]
            if isinstance(inner, list):
                return {"indexes": {DEFAULT_COLLECTION: inner}}
            if isinstance(inner, dict) and inner and all(isinstance(v, list) for v in inner.values()):
                return {"indexes": inner}
        if isinstance(data, dict) and "indexes" not in data:
            return {"indexes": data}
        return data


class DocumentSizeSpec(FrozenModel):
    target_bytes: int = 512


class CollectionDatasetSpec(FrozenModel):
    count: int = 1000
    document_size: DocumentSizeSpec | None = None
    fields: dict[str, FieldSpec | str] = Field(default_factory=dict)
    primary: bool = False


class ExternalGeneratorConfig(FrozenModel):
    """Shell-command contract for an external data-generator repository."""

    command: str
    working_dir: str | None = None
    timeout_seconds: int = 1800
    env: dict[str, str] = Field(default_factory=dict)
    reuse_if_present: bool = True
    count_tolerance: float = 0.05
    drop_after_run: bool = False


class DatasetConfig(FrozenModel):
    mode: Literal["synthetic", "existing", "external"] = "synthetic"
    seed: int = 42
    collection: str | None = None
    collections: dict[str, CollectionDatasetSpec] = Field(default_factory=dict)
    relationships: list[str] = Field(default_factory=list)
    # Existing multi-DB scale map: nominal document count → database name.
    # Same collection name is read from each mapped database.
    scales: dict[int, str] = Field(default_factory=dict)
    # External mode: one collection per scale, materialized by `generator`.
    collection_template: str = "{collection}_{documents}"
    generator: ExternalGeneratorConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _single_collection_shorthand(cls, data: Any) -> Any:
        """Allow `dataset: {count: N, fields: {...}}` without naming a collection."""
        if (
            isinstance(data, dict)
            and "collections" not in data
            and ("fields" in data or "count" in data)
        ):
            data = dict(data)
            spec = {
                key: data.pop(key)
                for key in ("count", "document_size", "fields", "primary")
                if key in data
            }
            data["collections"] = {DEFAULT_COLLECTION: spec}
        return data

    @field_validator("scales", mode="before")
    @classmethod
    def _coerce_scales(cls, value: Any) -> dict[int, str]:
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise ValueError("dataset.scales must be a mapping of document count → database")
        out: dict[int, str] = {}
        for key, database in value.items():
            size = int(key)
            name = str(database).strip()
            if size < 0:
                raise ValueError("dataset.scales keys must be non-negative document counts")
            if not name:
                raise ValueError("dataset.scales values must be non-empty database names")
            out[size] = name
        return out

    @model_validator(mode="after")
    def _mode_constraints(self) -> "DatasetConfig":
        if self.mode == "external" and self.generator is None:
            raise ValueError("dataset.mode 'external' requires a generator block")
        if self.scales and self.mode != "existing":
            raise ValueError("dataset.scales requires mode 'existing'")
        return self

    @property
    def has_scale_map(self) -> bool:
        return bool(self.scales)

    def scale_database(self, documents: int) -> str | None:
        if not self.scales:
            return None
        return self.scales.get(int(documents))


class QueryConfig(FrozenModel):
    id: str
    operation: Literal["find", "aggregate", "count", "distinct"] = "find"
    # Optional: defaults to the resolved primary collection of the target.
    collection: str | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    projection: dict[str, Any] | None = None
    sort: dict[str, int] | list[tuple[str, int]] | None = None
    limit: int | None = None
    pipeline: list[dict[str, Any]] | None = None
    key: str | None = None


class ParameterSpec(FrozenModel):
    type: Literal["dataset_value", "literal", "datetime_range", "integer", "float", "string"] = (
        "dataset_value"
    )
    field: str | None = None
    strategy: str = "random_existing"
    value: Any = None
    range: list[Any] | None = None
    min: float | None = None
    max: float | None = None

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        allowed = {
            "random_existing",
            "fixed",
            "selectivity_targeted",
            "hot_value",
            "cold_value",
            "percentile",
            "range",
        }
        if value not in allowed:
            raise ValueError(f"Unsupported parameter strategy '{value}'. Allowed: {sorted(allowed)}")
        return value


class ConnectionPoolConfig(FrozenModel):
    max_size: int = 100


class WorkloadConfig(FrozenModel):
    mode: Literal["closed_loop", "fixed_duration", "fixed_request_count"] = "closed_loop"
    duration_seconds: int | None = 60
    request_count: int | None = None
    concurrency: int = 1
    think_time_ms: int = 0
    timeout_ms: int = 30_000
    warmup_queries: int = 20
    explain_samples: int = 5
    connection_pool: ConnectionPoolConfig = Field(default_factory=ConnectionPoolConfig)


class DimensionSpec(FrozenModel):
    values: list[Any]


class ExperimentDimensions(FrozenModel):
    documents: DimensionSpec | None = None
    selectivity: DimensionSpec | None = None
    concurrency: DimensionSpec | None = None
    cache_state: DimensionSpec | None = None
    document_size: DimensionSpec | None = None
    result_cardinality: DimensionSpec | None = None
    array_cardinality: DimensionSpec | None = None
    range_width: DimensionSpec | None = None
    field_cardinality: DimensionSpec | None = None
    value_skew: DimensionSpec | None = None
    payload_size: DimensionSpec | None = None
    sort_size: DimensionSpec | None = None
    lookup_fan_out: DimensionSpec | None = None
    number_of_shards: DimensionSpec | None = None

    def as_dict(self) -> dict[str, list[Any]]:
        out: dict[str, list[Any]] = {}
        for name in type(self).model_fields:
            spec = getattr(self, name)
            if spec is not None:
                out[name] = list(spec.values)
        return out


class LatencySLO(FrozenModel):
    p95_ms: float
    p99_ms: float | None = None


class ErrorRateSLO(FrozenModel):
    maximum: float = 0.01


class SLOConfig(FrozenModel):
    latency: LatencySLO
    error_rate: ErrorRateSLO = Field(default_factory=ErrorRateSLO)
    green_fraction: float = 0.70


class ExecutionSettings(FrozenModel):
    warmup_queries: int = 20
    explain_samples: int = 5
    repetitions: int = 3
    boundary_uncertainty: float = 0.15
    change_point_multiplier: float = 2.0
    max_refinement_rounds: int = 3
    insert_batch_size: int = 1000
    runs_dir: str = "runs"


class ExperimentConfig(FrozenModel):
    id: str
    model: str | None = None
    models: list[str] = Field(default_factory=list)
    dataset: str | None = None
    datasets: dict[str, str] = Field(default_factory=dict)
    query: str
    queries: dict[str, str] = Field(default_factory=dict)
    indexes: str | dict[str, str] | None = None
    workload: str | None = None
    slo: str | None = None
    environment: str | None = None
    parameters: str | None = None
    dimensions: ExperimentDimensions
    repetitions: int = 3
    fixed: dict[str, Any] = Field(default_factory=dict)
    # Shortcut for pointing the suite at data that already exists in the cluster.
    database: str | None = None
    collection: str | None = None
    # Scales to project (extrapolate) curves for without measuring them.
    project_documents: list[int] = Field(default_factory=list)

    @property
    def targets_existing(self) -> bool:
        return self.collection is not None

    @model_validator(mode="after")
    def _models(self) -> "ExperimentConfig":
        names = list(self.models)
        if self.model and self.model not in names:
            names = [self.model, *names]
        if not names:
            raise ValueError("Experiment must specify model or models")
        if not self.dataset and not self.collection:
            raise ValueError("Experiment must specify dataset, or collection for existing data")
        self.models = names
        return self


class TargetConfig(FrozenModel):
    """One common place to declare where the engine points."""

    uri: str | None = None
    uri_env: str | None = None
    database: str | None = None
    collection: str | None = None


class ProjectConfig(FrozenModel):
    name: str
    environment: str | None = None
    default_slo: str | None = None
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    target: TargetConfig | None = None


class QueryBundle(FrozenModel):
    query: QueryConfig
    parameters: dict[str, ParameterSpec] = Field(default_factory=dict)


def parse_field_spec(raw: FieldSpec | str | dict[str, Any]) -> FieldSpec:
    if isinstance(raw, FieldSpec):
        return raw
    if isinstance(raw, str):
        return FieldSpec(type=raw)
    return FieldSpec.model_validate(raw)


def expand_fields(fields: dict[str, FieldSpec | str | dict[str, Any]]) -> dict[str, FieldSpec]:
    return {name: parse_field_spec(spec) for name, spec in fields.items()}


def primary_collection(
    dataset: "DatasetConfig | None" = None,
    model: "ModelConfig | None" = None,
    query_collection: str | None = None,
) -> str:
    """Resolve the logical collection a query targets when it does not name one.

    Priority: explicit `primary: true` on a dataset collection, then the sole
    collection when there is exactly one, then the query's own collection,
    then DEFAULT_COLLECTION.
    """
    if dataset is not None:
        if dataset.mode != "synthetic" and dataset.collection:
            return dataset.collection
        flagged = [name for name, spec in dataset.collections.items() if spec.primary]
        if flagged:
            return flagged[0]
        if len(dataset.collections) == 1:
            return next(iter(dataset.collections))
    if model is not None and len(model.collections) == 1:
        return next(iter(model.collections))
    if query_collection:
        return query_collection
    return DEFAULT_COLLECTION


FieldSpec.model_rebuild()
