"""Turn a UI run request into a spec dict for compile_spec_data."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from perf_envelope.config.loader import ResolvedExperiment
from perf_envelope.exceptions import ConfigError
from perf_envelope.query.parser import query_from_mongo_json


class AdvancedOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: Literal["existing", "synthetic", "external"] = "existing"
    documents: list[int] | str | None = None
    document_size: list[int] | str | None = None
    concurrency: list[int] | str | None = None
    selectivity: list[float] | str | None = None
    cache_state: list[str] | str | None = None
    # Existing multi-DB: nominal size → database name (JSON object or dict).
    scales: dict[int, str] | dict[str, str] | str | None = None
    slo_p95: float = 100
    duration_seconds: int = 12
    repetitions: int = 2
    max_refinement_rounds: int = 0
    generator_command: str | None = None
    generator_working_dir: str | None = None
    allow_external_writes: bool = False
    synthetic_seed: int = 42
    synthetic_count: int | None = None
    synthetic_fields: dict[str, Any] | None = None
    second_query: Any | None = None
    second_model: str = "alt"


def parse_int_list(value: list[int] | str | None) -> list[int] | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return [int(item) for item in value]
    parts = [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]
    if not parts:
        return None
    return [int(part) for part in parts]


def parse_str_list(value: list[str] | str | None) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    parts = [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]
    return parts or None


def parse_float_list(value: list[float] | str | None) -> list[float] | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return [float(item) for item in value]
    parts = [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]
    if not parts:
        return None
    return [float(part) for part in parts]


def parse_scales(value: dict[int, str] | dict[str, str] | str | None) -> dict[int, str] | None:
    if value in (None, ""):
        return None
    raw: Any = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid scales JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("scales must be a JSON object of document count → database")
    if not raw:
        return None
    out: dict[int, str] = {}
    for key, database in raw.items():
        size = int(key)
        name = str(database).strip()
        if not name:
            raise ConfigError("scales values must be non-empty database names")
        out[size] = name
    return out


DEFAULT_SYNTHETIC_FIELDS: dict[str, Any] = {
    "group_id": {
        "type": "integer",
        "cardinality": 200,
        "distribution": "zipf",
        "alpha": 1.3,
    },
    "status": {
        "type": "categorical",
        "values": {"Quoted": 0.7, "Ineligible": 0.2, "Failed": 0.1},
    },
    "created_at": {"type": "datetime", "distribution": "uniform", "start": "365d"},
    "amount": {"type": "float", "min": 1, "max": 10000},
}


DEFAULT_QUOTES_SCALES: dict[int, str] = {
    10_000: "mi_transformation",
    50_000: "mi_quotes_50000",
    100_000: "mi_quotes_100000",
}


class RunRequest(BaseModel):
    database: str
    collection: str
    query: Any
    ack_non_production: bool = False
    advanced: AdvancedOptions | None = None


def build_spec(
    *,
    uri: str,
    database: str,
    collection: str,
    query: Any,
    advanced: AdvancedOptions | None = None,
) -> dict[str, Any]:
    if not database:
        raise ConfigError("Database is required")
    if not collection:
        raise ConfigError("Collection is required")
    adv = advanced or AdvancedOptions()
    scales = parse_scales(adv.scales)
    mode = adv.mode
    if scales:
        mode = "existing"
    bundle = query_from_mongo_json(query, collection)
    concurrency = parse_int_list(adv.concurrency) or [1, 8]
    selectivity = parse_float_list(adv.selectivity) or [0.01, 0.1]
    cache_state = parse_str_list(adv.cache_state) or ["hot", "cold"]
    documents = parse_int_list(adv.documents)
    if scales and not documents:
        documents = sorted(scales)
    document_size = parse_int_list(adv.document_size)
    sweep: dict[str, Any] = {
        "concurrency": concurrency,
        "selectivity": selectivity,
        "cache_state": cache_state,
    }
    if documents:
        sweep["documents"] = documents
    # Document-size padding only applies when the engine synthesizes documents.
    if document_size and mode == "synthetic":
        sweep["document_size"] = document_size
    elif mode == "synthetic" and not document_size:
        sweep["document_size"] = [512, 2048]

    spec: dict[str, Any] = {
        "name": "ui-run",
        "target": {"uri": uri, "database": database, "collection": collection},
        "query": bundle.query.model_dump(exclude_none=True),
        "parameters": {key: value.model_dump() for key, value in bundle.parameters.items()},
        "slo": {"p95_ms": adv.slo_p95},
        "workload": {"duration_seconds": adv.duration_seconds, "warmup_queries": 10},
        "execution": {
            "repetitions": adv.repetitions,
            "max_refinement_rounds": adv.max_refinement_rounds,
        },
        "safety": {"non_production": True},
        "sweep": sweep,
    }

    if mode == "existing":
        data: dict[str, Any] = {"mode": "existing", "collection": collection}
        if scales:
            data["scales"] = scales
            if documents:
                missing = [size for size in documents if size not in scales]
                if missing:
                    raise ConfigError(
                        "documents sweep includes sizes without scales entries: "
                        + ", ".join(str(size) for size in missing)
                    )
        spec["data"] = data
    elif mode == "synthetic":
        count = adv.synthetic_count or (documents[0] if documents else 1000)
        fields = adv.synthetic_fields or DEFAULT_SYNTHETIC_FIELDS
        spec["data"] = {
            "mode": "synthetic",
            "seed": adv.synthetic_seed,
            "count": count,
            "fields": fields,
        }
        spec["sweep"].setdefault("documents", [count])
    else:
        if not adv.generator_command:
            raise ConfigError("External mode requires a generator command")
        if not documents:
            raise ConfigError("External mode requires a documents sweep")
        generator: dict[str, Any] = {
            "command": adv.generator_command,
            "reuse_if_present": True,
        }
        if adv.generator_working_dir:
            generator["working_dir"] = adv.generator_working_dir
        spec["data"] = {
            "mode": "external",
            "collection": collection,
            "collection_template": "{collection}_{documents}",
            "generator": generator,
        }
    return spec


def with_second_model(
    resolved: ResolvedExperiment,
    second_query: Any,
    collection: str,
    name: str = "alt",
) -> ResolvedExperiment:
    if second_query in (None, "", {}):
        return resolved
    bundle = query_from_mongo_json(second_query, collection)
    query = bundle.query.model_copy(update={"id": name, "collection": resolved.query.collection})
    models = list(resolved.experiment.models)
    if name not in models:
        models.append(name)
    experiment = resolved.experiment.model_copy(update={"models": models})
    primary_model = next(iter(resolved.models.values()))
    primary_index = next(iter(resolved.indexes.values()))
    return replace(
        resolved,
        experiment=experiment,
        models={**resolved.models, name: primary_model.model_copy(update={"name": name})},
        queries={**resolved.queries, name: query},
        datasets={**resolved.datasets, name: resolved.dataset},
        parameters_by_model={**resolved.parameters_by_model, name: bundle.parameters},
        indexes={**resolved.indexes, name: primary_index},
    )
