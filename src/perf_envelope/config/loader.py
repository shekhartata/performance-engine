"""Load and resolve a portable project directory into typed configs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from perf_envelope.config.models import (
    DatasetConfig,
    EnvironmentConfig,
    ExecutionSettings,
    ExperimentConfig,
    IndexConfig,
    ModelConfig,
    ParameterSpec,
    ProjectConfig,
    QueryBundle,
    QueryConfig,
    SLOConfig,
    WorkloadConfig,
    primary_collection,
)
from perf_envelope.exceptions import ConfigError

T = TypeVar("T", bound=BaseModel)

KIND_DIRS = {
    "environment": "environments",
    "model": "models",
    "dataset": "datasets",
    "indexes": "indexes",
    "query": "queries",
    "workload": "workloads",
    "experiment": "experiments",
    "slo": "slo",
    "parameters": "parameters",
}

UNWRAP_KEYS = {
    "environment",
    "model",
    "dataset",
    "indexes",
    "query",
    "workload",
    "experiment",
    "slo",
    "project",
    "parameters",
}


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"File not found: {path}")
    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"YAML root must be a mapping: {path}")
    return data


def unwrap(data: dict[str, Any], expected: str | None = None) -> dict[str, Any]:
    if expected and expected in data and isinstance(data[expected], dict):
        inner = dict(data[expected])
        leftover = {k: v for k, v in data.items() if k != expected}
        inner.update({k: v for k, v in leftover.items() if k not in inner})
        return inner
    keys = [k for k in data if k in UNWRAP_KEYS]
    if len(keys) == 1 and isinstance(data[keys[0]], dict) and len(data) == 1:
        return data[keys[0]]
    return data


def parse_model(model_cls: type[T], data: dict[str, Any], expected: str | None = None) -> T:
    try:
        return model_cls.model_validate(unwrap(data, expected))
    except Exception as exc:  # noqa: BLE001 — surface pydantic errors as ConfigError
        raise ConfigError(f"Invalid {model_cls.__name__}: {exc}") from exc


@dataclass
class ResolvedExperiment:
    project_dir: Path
    project: ProjectConfig
    environment: EnvironmentConfig
    experiment: ExperimentConfig
    dataset: DatasetConfig
    datasets: dict[str, DatasetConfig]
    query: QueryConfig
    queries: dict[str, QueryConfig]
    parameters: dict[str, ParameterSpec]
    parameters_by_model: dict[str, dict[str, ParameterSpec]]
    workload: WorkloadConfig
    slo: SLOConfig
    models: dict[str, ModelConfig]
    indexes: dict[str, IndexConfig]
    execution: ExecutionSettings
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def model_names(self) -> list[str]:
        return list(self.experiment.models)


class ProjectLoader:
    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir).resolve()
        if not self.project_dir.exists():
            raise ConfigError(f"Project directory not found: {self.project_dir}")
        project_path = self.project_dir / "project.yaml"
        raw = unwrap(load_yaml(project_path), "project")
        if "name" not in raw:
            raw["name"] = self.project_dir.name
        self.project = parse_model(ProjectConfig, raw, "project")

    def resolve_named(self, kind: str, name: str | None) -> Path:
        if not name:
            raise ConfigError(f"Missing {kind} reference")
        candidate = Path(name)
        if candidate.suffix in {".yaml", ".yml"} and candidate.is_file():
            return candidate.resolve()
        abs_candidate = (self.project_dir / name).resolve()
        if abs_candidate.is_file():
            return abs_candidate
        folder = self.project_dir / KIND_DIRS[kind]
        for suffix in (".yaml", ".yml", ""):
            path = folder / f"{name}{suffix}" if suffix else folder / name
            if path.is_file():
                return path
        raise ConfigError(f"Cannot resolve {kind} '{name}' under {folder}")

    def load_environment(self, name: str | None = None) -> EnvironmentConfig:
        resolved = name or self.project.environment
        if not resolved:
            raise ConfigError("No environment specified")
        data = load_yaml(self.resolve_named("environment", resolved))
        env = parse_model(EnvironmentConfig, data, "environment")
        # Project-level safety wins when stricter / explicitly set.
        env = env.model_copy(update={"safety": self.project.safety})
        return env

    def load_model(self, name: str) -> ModelConfig:
        data = unwrap(load_yaml(self.resolve_named("model", name)), "model")
        if "name" not in data:
            data["name"] = Path(name).stem
        return parse_model(ModelConfig, data)

    def load_dataset(self, name: str) -> DatasetConfig:
        return parse_model(DatasetConfig, load_yaml(self.resolve_named("dataset", name)), "dataset")

    def load_indexes(self, name: str) -> IndexConfig:
        return parse_model(IndexConfig, load_yaml(self.resolve_named("indexes", name)))

    def load_query_bundle(self, name: str, parameters_ref: str | None = None) -> QueryBundle:
        raw = load_yaml(self.resolve_named("query", name))
        query_data = unwrap(raw, "query")
        params_raw = {}
        if isinstance(query_data, dict) and "parameters" in query_data:
            params_raw = query_data.pop("parameters") or {}
        elif raw.get("parameters"):
            params_raw = raw.get("parameters") or {}
        query = parse_model(QueryConfig, query_data)
        if parameters_ref:
            params_raw = unwrap(
                load_yaml(self.resolve_named("parameters", parameters_ref)), "parameters"
            )
        parameters = {
            key: ParameterSpec.model_validate(value) for key, value in (params_raw or {}).items()
        }
        return QueryBundle(query=query, parameters=parameters)

    def load_workload(self, name: str | None) -> WorkloadConfig:
        if not name:
            return WorkloadConfig()
        return parse_model(WorkloadConfig, load_yaml(self.resolve_named("workload", name)), "workload")

    def load_slo(self, name: str | None) -> SLOConfig:
        resolved = name or self.project.default_slo
        if not resolved:
            raise ConfigError("No SLO specified")
        return parse_model(SLOConfig, load_yaml(self.resolve_named("slo", resolved)), "slo")

    def load_experiment(self, name: str) -> ExperimentConfig:
        return parse_model(
            ExperimentConfig, load_yaml(self.resolve_named("experiment", name)), "experiment"
        )

    def resolve_experiment(
        self,
        experiment_name: str,
        *,
        database: str | None = None,
        collection: str | None = None,
    ) -> ResolvedExperiment:
        experiment = self.load_experiment(experiment_name)
        # CLI overrides win over the experiment file, which wins over project.target.
        overrides: dict[str, Any] = {}
        if database:
            overrides["database"] = database
        if collection:
            overrides["collection"] = collection
        target = self.project.target
        if target:
            if not overrides.get("database") and not experiment.database and target.database:
                overrides["database"] = target.database
            if not overrides.get("collection") and not experiment.collection and target.collection:
                overrides["collection"] = target.collection
        if overrides:
            experiment = experiment.model_copy(update=overrides)
        if experiment.targets_existing:
            # One physical collection means the candidate models would run identical
            # workloads, so collapse to a single model.
            experiment = experiment.model_copy(
                update={"models": experiment.models[:1], "model": experiment.models[0]}
            )
        environment = self.load_environment(experiment.environment)
        if target and target.uri_env:
            environment = environment.model_copy(
                update={
                    "connection": environment.connection.model_copy(
                        update={"uri_env": target.uri_env}
                    )
                }
            )
        if experiment.database:
            environment = environment.model_copy(
                update={
                    "connection": environment.connection.model_copy(
                        update={"database": experiment.database}
                    )
                }
            )
        existing_dataset: DatasetConfig | None = None
        if experiment.targets_existing:
            existing_dataset = DatasetConfig(mode="existing", collection=experiment.collection)
        dataset = existing_dataset or self.load_dataset(experiment.dataset)
        models = {name: self.load_model(name) for name in experiment.models}
        first_model = models.get(experiment.models[0]) if experiment.models else None
        bundle = self.load_query_bundle(experiment.query, experiment.parameters)
        if experiment.collection:
            bundle = bundle.model_copy(
                update={
                    "query": bundle.query.model_copy(update={"collection": experiment.collection})
                }
            )
        elif bundle.query.collection is None:
            bundle = bundle.model_copy(
                update={
                    "query": bundle.query.model_copy(
                        update={"collection": primary_collection(dataset, first_model)}
                    )
                }
            )
        workload = self.load_workload(experiment.workload)
        slo = self.load_slo(experiment.slo)
        indexes: dict[str, IndexConfig] = {}
        for model_name in experiment.models:
            idx_ref: str | None
            if isinstance(experiment.indexes, dict):
                idx_ref = experiment.indexes.get(model_name)
            else:
                idx_ref = experiment.indexes
            if idx_ref is None:
                idx_ref = model_name
            try:
                indexes[model_name] = self.load_indexes(idx_ref)
            except ConfigError:
                # Existing collections are read-only, so index definitions are optional.
                if len(experiment.models) == 1 and not experiment.targets_existing:
                    raise
                indexes[model_name] = IndexConfig()
        datasets: dict[str, DatasetConfig] = {}
        queries: dict[str, QueryConfig] = {}
        parameters_by_model: dict[str, dict[str, ParameterSpec]] = {}
        for model_name in experiment.models:
            if existing_dataset is not None:
                datasets[model_name] = existing_dataset
                queries[model_name] = bundle.query
                parameters_by_model[model_name] = bundle.parameters
                continue
            ds_name = experiment.datasets.get(model_name, experiment.dataset)
            datasets[model_name] = self.load_dataset(ds_name)
            q_name = experiment.queries.get(model_name, experiment.query)
            q_bundle = self.load_query_bundle(q_name, experiment.parameters)
            if q_bundle.query.collection is None:
                q_bundle = q_bundle.model_copy(
                    update={
                        "query": q_bundle.query.model_copy(
                            update={
                                "collection": primary_collection(
                                    datasets[model_name], models.get(model_name)
                                )
                            }
                        )
                    }
                )
            queries[model_name] = q_bundle.query
            parameters_by_model[model_name] = q_bundle.parameters
        return ResolvedExperiment(
            project_dir=self.project_dir,
            project=self.project,
            environment=environment,
            experiment=experiment,
            dataset=dataset,
            datasets=datasets,
            query=bundle.query,
            queries=queries,
            parameters=bundle.parameters,
            parameters_by_model=parameters_by_model,
            workload=workload,
            slo=slo,
            models=models,
            indexes=indexes,
            execution=self.project.execution.model_copy(
                update={"repetitions": experiment.repetitions}
            ),
        )


def list_yaml_stems(project_dir: Path, kind: str) -> list[str]:
    folder = project_dir / KIND_DIRS[kind]
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob("*.y*ml"))
