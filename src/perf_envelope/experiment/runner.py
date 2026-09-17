"""Execute experiment cells: dataset → indexes → cache → warmup → measure → persist."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

from perf_envelope.config.loader import ResolvedExperiment
from perf_envelope.config.models import DEFAULT_COLLECTION, DatasetConfig, primary_collection
from perf_envelope.dataset.external import materialize, render_collection_name
from perf_envelope.dataset.generator import generate_into_mongo
from perf_envelope.dataset.guardrails import assert_within_limit, estimate_storage
from perf_envelope.dataset.scaler import scale_dataset
from perf_envelope.environment.discovery import discover
from perf_envelope.environment.mongodb import MongoSession, connect, resolve_uri
from perf_envelope.environment.safety import (
    CreationManifest,
    assert_non_production,
    managed_collection_name,
)
from perf_envelope.exceptions import ExecutionError, SafetyError
from perf_envelope.experiment.matrix import ExperimentCell
from perf_envelope.experiment.planner import ExperimentPlanner
from perf_envelope.experiment.refinement import propose_refinement
from perf_envelope.models.indexes import ensure_indexes
from perf_envelope.query.canonicalizer import canonical_form, shape_id
from perf_envelope.query.parameters import QueryParameterGenerator
from perf_envelope.storage.runs import ExperimentRepository
from perf_envelope.telemetry.explain import explain_query, summarize_explains
from perf_envelope.telemetry.resources import capture_resources
from perf_envelope.workload.cache import HOT, normalize_cache_state
from perf_envelope.workload.concurrency import WorkloadExecutor, WorkloadMetrics

console = Console()


class ExperimentRunner:
    def __init__(
        self,
        resolved: ResolvedExperiment,
        *,
        acknowledged: bool,
        override_storage: bool = False,
        dry_run: bool = False,
        allow_external_writes: bool = False,
        repo: ExperimentRepository | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.resolved = resolved
        self.acknowledged = acknowledged
        self.override_storage = override_storage
        self.dry_run = dry_run
        self.allow_external_writes = allow_external_writes
        self.repo = repo or ExperimentRepository(resolved.execution.runs_dir)
        self.planner = ExperimentPlanner(resolved)
        self.on_progress = on_progress

    def run(self, run_id: str | None = None) -> str:
        safety = self.resolved.environment.safety
        assert_non_production(safety, self.acknowledged)
        if self._uses_external() and not self.allow_external_writes:
            raise SafetyError(
                "External generator writes collections outside the perfenv_ prefix. "
                "Re-run with --allow-external-writes to confirm."
            )
        run_dir = self.repo.create(run_id)
        manifest = CreationManifest(project=self.resolved.project.name)
        session = connect(
            self.resolved.environment,
            max_pool_size=self.resolved.workload.connection_pool.max_size,
        )
        try:
            env_meta = discover(session)
            self.repo.write_json(run_dir, "environment.json", env_meta)
            self.repo.write_json(
                run_dir,
                "configuration/experiment.json",
                self.resolved.experiment.model_dump(),
            )
            self.repo.write_json(
                run_dir,
                "configuration/query.json",
                {
                    "query": self.resolved.query.model_dump(),
                    "canonical": canonical_form(self.resolved.query),
                    "shape_id": shape_id(self.resolved.query),
                },
            )
            self.repo.write_json(run_dir, "configuration/slo.json", self.resolved.slo.model_dump())
            observations: list[dict[str, Any]] = []
            explains: list[dict[str, Any]] = []
            cells = self.planner.coarse_cells()
            for model_name in self.resolved.model_names:
                observations.extend(
                    self._run_model(session, run_dir, model_name, cells, manifest, explains)
                )
            for row in observations:
                row["run_id"] = run_dir.name
            self.repo.save_observations(run_dir, observations)
            self.repo.write_json(run_dir, "explain.json", explains)
            self.repo.write_json(run_dir, "manifest.json", manifest.to_dict())
            self.repo.write_json(
                run_dir,
                "provenance.json",
                self.repo.provenance(
                    {
                        "mongodb_version": env_meta.get("mongodb_version"),
                        "environment": env_meta,
                        "dataset_seed": self.resolved.dataset.seed,
                        "query_shape": canonical_form(self.resolved.query),
                    }
                ),
            )
        finally:
            session.close()
        console.print(f"[green]Run complete:[/green] {run_dir}")
        return run_dir.name

    def _emit_progress(self, payload: dict[str, Any]) -> None:
        if self.on_progress is None:
            return
        self.on_progress(payload)

    def _uses_external(self) -> bool:
        if self.resolved.dataset.mode == "external":
            return True
        return any(ds.mode == "external" for ds in self.resolved.datasets.values())

    def _run_model(
        self,
        session: MongoSession,
        run_dir,
        model_name: str,
        coarse_cells: list[ExperimentCell],
        manifest: CreationManifest,
        explains: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        completed: dict[tuple, ExperimentCell] = {}
        p95_by_key: dict[tuple, float] = {}
        generated_sizes: set[int] = set()
        dataset = self.resolved.datasets.get(model_name, self.resolved.dataset)
        existing = dataset.mode == "existing"
        physical = self._physical_names(model_name)

        if existing:
            pending = self._existing_cells(session, model_name, coarse_cells, physical)
            rounds = 1
        else:
            pending = list(coarse_cells)
            rounds = self.resolved.execution.max_refinement_rounds + 1

        for round_idx in range(rounds):
            if not pending:
                break
            console.print(
                f"[bold]{model_name}[/bold] round {round_idx + 1}: {len(pending)} cells"
            )
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("cells", total=len(pending))
                for cell in pending:
                    self._emit_progress(
                        {
                            "phase": "cell",
                            "model": model_name,
                            "documents": cell.documents,
                            "concurrency": cell.concurrency,
                            "selectivity": cell.selectivity,
                            "cache_state": cell.cache_state,
                            "database": self._scale_database_name(dataset, cell),
                            "round": round_idx + 1,
                        }
                    )
                    physical = self._physical_names(model_name, cell)
                    if cell.documents not in generated_sizes:
                        self._prepare_dataset(
                            session, model_name, cell, physical, manifest, run_dir
                        )
                        generated_sizes.add(cell.documents)
                    for rep in range(self.resolved.execution.repetitions):
                        obs, explain = self._run_cell(
                            session, model_name, cell, physical, rep
                        )
                        observations.append(obs)
                        explains.append(explain)
                    p95s = [
                        row["p95_ms"]
                        for row in observations
                        if row["model_id"] == model_name
                        and row["dataset_size"] == cell.documents
                        and row["selectivity"] == cell.selectivity
                        and row["concurrency"] == cell.concurrency
                        and row["cache_state"] == cell.cache_state
                    ]
                    p95_by_key[cell.key()] = float(sum(p95s) / len(p95s)) if p95s else 0.0
                    completed[cell.key()] = cell
                    progress.advance(task)
            if existing:
                # Dataset size is fixed by the collection, so there is no boundary to refine.
                break
            pending = propose_refinement(
                completed.values(),
                p95_by_key,
                self.resolved.slo.latency.p95_ms,
                uncertainty=self.resolved.execution.boundary_uncertainty,
                green_fraction=self.resolved.slo.green_fraction,
            )
            pending = [c for c in pending if c.key() not in completed]
        if dataset.mode == "external" and dataset.generator and dataset.generator.drop_after_run:
            self._drop_external(session, model_name, generated_sizes)
        return observations

    def _existing_cells(
        self,
        session: MongoSession,
        model_name: str,
        coarse_cells: list[ExperimentCell],
        physical: dict[str, str],
    ) -> list[ExperimentCell]:
        """Resolve existing collections; keep a documents axis when scales are set."""
        dataset = self.resolved.datasets.get(model_name, self.resolved.dataset)
        if dataset.has_scale_map:
            return self._existing_scale_cells(session, model_name, coarse_cells, physical)

        query = self.resolved.queries.get(model_name, self.resolved.query)
        logical = query.collection or next(iter(physical))
        name = physical[logical]
        if name not in session.database.list_collection_names():
            raise ExecutionError(
                f"Collection '{name}' not found in database "
                f"'{session.database.name}'. Existing-dataset mode does not create data."
            )
        actual = int(session.database[name].estimated_document_count())
        console.print(
            f"[cyan]{model_name}[/cyan] using existing collection "
            f"{session.database.name}.{name} ({actual} documents); dataset-size sweep disabled"
        )
        collapsed: dict[tuple, ExperimentCell] = {}
        for cell in coarse_cells:
            replaced = ExperimentCell(
                documents=actual,
                selectivity=cell.selectivity,
                concurrency=cell.concurrency,
                cache_state=cell.cache_state,
                extras=dict(cell.extras),
            )
            collapsed.setdefault(replaced.key(), replaced)
        return list(collapsed.values())

    def _existing_scale_cells(
        self,
        session: MongoSession,
        model_name: str,
        coarse_cells: list[ExperimentCell],
        physical: dict[str, str],
    ) -> list[ExperimentCell]:
        """Validate each scale database and keep the nominal documents axis."""
        dataset = self.resolved.datasets.get(model_name, self.resolved.dataset)
        query = self.resolved.queries.get(model_name, self.resolved.query)
        logical = query.collection or next(iter(physical))
        name = physical[logical]
        cells: list[ExperimentCell] = []
        seen: set[tuple] = set()
        for cell in coarse_cells:
            db_name = dataset.scale_database(cell.documents)
            if not db_name:
                raise ExecutionError(
                    f"No dataset.scales entry for documents={cell.documents}. "
                    f"Known scales: {sorted(dataset.scales)}"
                )
            database = session.client[db_name]
            if name not in database.list_collection_names():
                raise ExecutionError(
                    f"Collection '{name}' not found in scale database '{db_name}' "
                    f"(documents={cell.documents})."
                )
            actual = int(database[name].estimated_document_count())
            console.print(
                f"[cyan]{model_name}[/cyan] scale {cell.documents}: "
                f"{db_name}.{name} ({actual} documents)"
            )
            key = cell.key()
            if key in seen:
                continue
            seen.add(key)
            extras = dict(cell.extras)
            extras["scale_database"] = db_name
            extras["measured_documents"] = actual
            cells.append(
                ExperimentCell(
                    documents=cell.documents,
                    selectivity=cell.selectivity,
                    concurrency=cell.concurrency,
                    cache_state=cell.cache_state,
                    extras=extras,
                )
            )
        return cells

    def _scale_database_name(self, dataset: DatasetConfig, cell: ExperimentCell) -> str | None:
        if not dataset.has_scale_map:
            return None
        return dataset.scale_database(cell.documents) or cell.extras.get("scale_database")

    def _database_for_cell(
        self,
        session: MongoSession,
        model_name: str,
        cell: ExperimentCell,
    ):
        dataset = self.resolved.datasets.get(model_name, self.resolved.dataset)
        db_name = self._scale_database_name(dataset, cell)
        if db_name:
            return session.client[db_name]
        return session.database

    def _logical_collection(self, model_name: str) -> str:
        dataset = self.resolved.datasets.get(model_name, self.resolved.dataset)
        query = self.resolved.queries.get(model_name, self.resolved.query)
        model = self.resolved.models.get(model_name)
        return (
            dataset.collection
            or query.collection
            or primary_collection(dataset, model)
            or DEFAULT_COLLECTION
        )

    def _physical_names(
        self, model_name: str, cell: ExperimentCell | None = None
    ) -> dict[str, str]:
        dataset = self.resolved.datasets.get(model_name, self.resolved.dataset)
        logical = self._logical_collection(model_name)
        query = self.resolved.queries.get(model_name, self.resolved.query)
        if dataset.mode == "existing":
            mapping = {logical: logical}
            if query.collection:
                mapping[query.collection] = logical
            return mapping
        if dataset.mode == "external":
            documents = cell.documents if cell is not None else 0
            physical = render_collection_name(
                dataset.collection_template,
                collection=logical,
                documents=documents,
            )
            mapping = {logical: physical}
            if query.collection:
                mapping[query.collection] = physical
            return mapping
        model = self.resolved.models[model_name]
        prefix = self.resolved.environment.safety.managed_prefix
        project = f"{self.resolved.project.name}_{model_name}"
        names = set(model.collections) | set(dataset.collections)
        if logical:
            names.add(logical)
        return {name: managed_collection_name(project, name, prefix) for name in names}

    def _prepare_dataset(
        self,
        session: MongoSession,
        model_name: str,
        cell: ExperimentCell,
        physical: dict[str, str],
        manifest: CreationManifest,
        run_dir=None,
    ) -> None:
        dataset = self.resolved.datasets.get(model_name, self.resolved.dataset)
        query = self.resolved.queries.get(model_name, self.resolved.query)
        primary = query.collection or self._logical_collection(model_name)
        if dataset.mode == "existing":
            return
        if dataset.mode == "external":
            if self.dry_run:
                return
            generator = dataset.generator
            cwd = None
            if generator and generator.working_dir:
                cwd = Path(generator.working_dir)
                if not cwd.is_absolute():
                    cwd = (self.resolved.project_dir / cwd).resolve()
            materialize(
                session,
                dataset,
                documents=cell.documents,
                collection=physical[primary],
                database=session.database.name,
                uri=resolve_uri(self.resolved.environment),
                seed=dataset.seed,
                model=model_name,
                log_dir=(Path(run_dir) / "generator") if run_dir is not None else None,
                working_dir=cwd,
                manifest=manifest,
            )
            return
        scaled = scale_dataset(dataset, primary, cell.documents)
        extra_size = cell.extras.get("document_size") or cell.extras.get("payload_size")
        if extra_size:
            spec = scaled.collections.get(primary)
            if spec and spec.document_size:
                spec.document_size.target_bytes = int(extra_size)
        estimate = estimate_storage(scaled, index_count=2)
        assert_within_limit(
            estimate,
            self.resolved.environment.safety,
            override=self.override_storage,
        )
        if self.dry_run:
            return
        generate_into_mongo(
            session.database,
            scaled,
            physical,
            batch_size=self.resolved.execution.insert_batch_size,
            manifest=manifest,
        )
        index_cfg = self.resolved.indexes.get(model_name)
        if index_cfg:
            for logical, specs in index_cfg.indexes.items():
                if logical in physical:
                    ensure_indexes(session.database, physical[logical], specs)

    def _drop_external(self, session: MongoSession, model_name: str, sizes: set[int]) -> None:
        for size in sizes:
            physical = self._physical_names(
                model_name, ExperimentCell(documents=size, selectivity=0, concurrency=1, cache_state="hot")
            )
            for name in set(physical.values()):
                if name in session.database.list_collection_names():
                    session.database[name].drop()
                    console.print(f"dropped external collection {name}")

    def _run_cell(
        self,
        session: MongoSession,
        model_name: str,
        cell: ExperimentCell,
        physical: dict[str, str],
        repetition: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        query = self.resolved.queries.get(model_name, self.resolved.query)
        parameters = self.resolved.parameters_by_model.get(model_name, self.resolved.parameters)
        logical = query.collection or self._logical_collection(model_name)
        database = self._database_for_cell(session, model_name, cell)
        collection = database[physical[logical]]
        param_gen = QueryParameterGenerator(
            parameters, rng=np.random.default_rng(self.resolved.dataset.seed + repetition)
        )
        for spec in parameters.values():
            if spec.field:
                try:
                    param_gen.preload(collection, spec.field)
                except Exception:  # noqa: BLE001
                    continue
        workload = self.resolved.workload.model_copy(
            update={
                "concurrency": cell.concurrency,
                "warmup_queries": self.resolved.execution.warmup_queries,
                "explain_samples": self.resolved.execution.explain_samples,
            }
        )
        executor = WorkloadExecutor(
            collection, query, workload, param_gen, selectivity=cell.selectivity
        )
        cache_state = normalize_cache_state(cell.cache_state)
        if cache_state == HOT:
            executor.warmup(cell.concurrency)
        metrics: WorkloadMetrics = executor.run(concurrency=cell.concurrency, include_warmup=False)
        explain_samples = []
        for _ in range(workload.explain_samples):
            explain_samples.append(
                explain_query(collection, query, param_gen.next(cell.selectivity))
            )
        explain_summary = summarize_explains(explain_samples)
        resources = capture_resources(database)
        dataset = self.resolved.datasets.get(model_name, self.resolved.dataset)
        document_size = 0
        primary_spec = dataset.collections.get(logical)
        if primary_spec and primary_spec.document_size:
            document_size = primary_spec.document_size.target_bytes
        document_size = int(cell.extras.get("document_size") or document_size or 0)
        observation = {
            "experiment_id": self.resolved.experiment.id,
            "run_id": None,
            "environment_id": database.name,
            "scale_database": database.name if dataset.has_scale_map else None,
            "model_id": model_name,
            "query_shape_id": shape_id(query),
            "dataset_size": cell.documents,
            "selectivity": cell.selectivity,
            "concurrency": cell.concurrency,
            "cache_state": cache_state,
            "document_size_bytes": document_size,
            "result_count": explain_summary.get("n_returned") or query.limit or 0,
            "repetition": repetition,
            **metrics.as_dict(),
            "n_returned": explain_summary.get("n_returned"),
            "keys_examined": explain_summary.get("keys_examined"),
            "docs_examined": explain_summary.get("docs_examined"),
            "keys_examined_per_returned": explain_summary.get("keys_examined_per_returned"),
            "docs_examined_per_returned": explain_summary.get("docs_examined_per_returned"),
            "resource_metrics": resources,
        }
        if cell.extras.get("measured_documents") is not None:
            observation["measured_documents"] = cell.extras["measured_documents"]
        explain_record = {
            "model_id": model_name,
            "cell": cell.as_dict(),
            "repetition": repetition,
            **explain_summary,
        }
        return observation, explain_record
