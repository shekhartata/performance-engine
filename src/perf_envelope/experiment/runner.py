"""Execute experiment cells: dataset → indexes → cache → warmup → measure → persist."""

from __future__ import annotations

import time
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
from perf_envelope.dataset.recipe import materialize_recipe
from perf_envelope.dataset.scaler import scale_dataset
from perf_envelope.environment.discovery import discover
from perf_envelope.environment.mongodb import MongoSession, connect, resolve_uri
from perf_envelope.environment.pool import SizedClientCache, measure_rtt_ms, required_pool_size
from perf_envelope.environment.safety import (
    CreationManifest,
    assert_non_production,
    managed_collection_name,
)
from perf_envelope.exceptions import ExecutionError, SafetyError
from perf_envelope.experiment.matrix import ExperimentCell
from perf_envelope.experiment.planner import ExperimentPlanner
from perf_envelope.experiment.refinement import propose_refinement
from perf_envelope.experiment.settings_log import cell_settings, host_info, run_level_settings
from perf_envelope.models.indexes import ensure_indexes
from perf_envelope.query.canonicalizer import canonical_form, shape_id
from perf_envelope.query.parameters import QueryParameterGenerator
from perf_envelope.storage.runs import ExperimentRepository
from perf_envelope.telemetry.explain import explain_query, summarize_explains
from perf_envelope.telemetry.resources import capture_resources
from perf_envelope.workload.cache import HOT, normalize_cache_state
from perf_envelope.workload.concurrency import WorkloadExecutor, WorkloadMetrics
from perf_envelope.workload.plan import parallel_width, render_plan, step_query

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
        self._cell_logs: list[dict[str, Any]] = []
        self._clients: SizedClientCache | None = None
        self._recipe_state = None
        self._rtt_ms: float | None = None

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
        self._clients = SizedClientCache(resolve_uri(self.resolved.environment))
        self._cell_logs = []
        try:
            self._rtt_ms = measure_rtt_ms(session.client)
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
                    "parameters": {
                        name: spec.model_dump() for name, spec in self.resolved.parameters.items()
                    },
                    "plans": self.resolved.plans,
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
            if self._recipe_state is not None:
                self.resolved.extra["recipe_state"] = self._recipe_state.as_dict()
            self.repo.write_json(
                run_dir,
                "configuration/run_settings.json",
                {
                    "run": run_level_settings(
                        self.resolved, rtt_ms=self._rtt_ms, host=host_info()
                    ),
                    "cells": self._cell_logs,
                    "rtt_ms": self._rtt_ms,
                    "host": host_info(),
                    "recipe": self.resolved.extra.get("recipe_state"),
                },
            )
        finally:
            if self._clients is not None:
                self._clients.close()
                self._clients = None
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
        self._ensure_recipe(session, manifest, coarse_cells)
        goal = self.resolved.experiment.goal
        axis = goal.axis if goal is not None else "documents"

        if existing:
            pending = self._existing_cells(session, model_name, coarse_cells, physical)
            # Document count is fixed by the collection. Other axes can still be refined.
            rounds = 1 if axis == "documents" else self.resolved.execution.max_refinement_rounds + 1
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
                        if _same_point(row, model_name, cell)
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
                axis=axis,
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
                bindings=dict(cell.bindings),
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
                    bindings=dict(cell.bindings),
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
        logical_db = self._database_for_cell(session, model_name, cell)
        variables = self._variables_for_cell(cell, logical_db.name)
        plan_raw = self.resolved.plans.get(model_name)
        rendered = render_plan(plan_raw, variables) if plan_raw else None
        parallel = parallel_width(rendered)
        pool_cfg = self.resolved.workload.connection_pool
        pool_size = (
            pool_cfg.max_size
            if pool_cfg.mode == "fixed"
            else required_pool_size(cell.concurrency, parallel, pool_cfg.headroom)
        )
        monitor = None
        if self._clients is not None:
            client, monitor = self._clients.client(pool_size)
            database = client[logical_db.name]
        else:
            database = logical_db
        if rendered is not None:
            collection = database[rendered.steps[0].collection]
        else:
            collection = database[physical[logical]]
        param_gen = QueryParameterGenerator(
            parameters, rng=np.random.default_rng(self.resolved.dataset.seed + repetition)
        )
        self._seed_parameters(param_gen, parameters, logical_db.name, cell)
        for spec in parameters.values():
            field_name = spec.field
            if field_name and not param_gen.cached_values.get(field_name):
                try:
                    param_gen.preload(collection, field_name)
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
            collection,
            query,
            workload,
            param_gen,
            selectivity=cell.selectivity,
            plan=rendered,
            database=database if rendered is not None else None,
        )
        cache_state = normalize_cache_state(cell.cache_state)
        if cache_state == HOT:
            executor.warmup(cell.concurrency)
        if monitor is not None:
            monitor.begin_measurement()
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        metrics: WorkloadMetrics = executor.run(concurrency=cell.concurrency, include_warmup=False)
        wall = max(time.perf_counter() - wall_started, 1e-9)
        client_cpu_pct = 100.0 * (time.process_time() - cpu_started) / wall
        pool_stats = monitor.snapshot() if monitor is not None else {
            "peak_in_use": 0,
            "connections_created": 0,
            "wait_p50_ms": 0.0,
            "wait_p95_ms": 0.0,
            "wait_max_ms": 0.0,
            "checkouts": 0,
        }
        sampled = _sample_summary(executor.sample_counts)
        explain_samples = []
        for _ in range(workload.explain_samples):
            params = param_gen.next(cell.selectivity)
            if rendered is not None:
                for step in rendered.steps:
                    explain_samples.append(
                        explain_query(database[step.collection], step_query(step), params)
                    )
            else:
                explain_samples.append(explain_query(collection, query, params))
        explain_summary = summarize_explains(explain_samples)
        resources = capture_resources(database)
        dataset = self.resolved.datasets.get(model_name, self.resolved.dataset)
        document_size = 0
        primary_spec = dataset.collections.get(logical)
        if primary_spec and primary_spec.document_size:
            document_size = primary_spec.document_size.target_bytes
        document_size = int(cell.extras.get("document_size") or document_size or 0)
        executed_rows = (
            int(round(metrics.returned_total / metrics.successes)) if metrics.successes else 0
        )
        collections = (
            [step.collection for step in rendered.steps]
            if rendered is not None
            else [collection.name]
        )
        cell_values = cell.variables()
        record = cell_settings(
            model=model_name,
            cell_values=cell_values,
            bindings=cell.bindings,
            database=database.name,
            collections=collections,
            rendered=rendered.as_dict() if rendered is not None else None,
            concurrency=cell.concurrency,
            parallel_steps=parallel,
            headroom=pool_cfg.headroom,
            pool_mode=pool_cfg.mode,
            fixed_pool_size=pool_cfg.max_size,
            pool_stats=pool_stats,
            warmup=cache_state == HOT,
            sampled_parameters=sampled,
            client_cpu_pct=client_cpu_pct,
            rtt_ms=self._rtt_ms,
        )
        record["repetition"] = repetition
        self._cell_logs.append(record)
        observation = {
            "experiment_id": self.resolved.experiment.id,
            "run_id": None,
            "environment_id": database.name,
            "scale_database": database.name if dataset.has_scale_map else None,
            "model_id": model_name,
            "plan": model_name if plan_raw else None,
            "query_shape_id": shape_id(query),
            "dataset_size": cell.documents,
            "selectivity": cell.selectivity,
            "concurrency": cell.concurrency,
            "cache_state": cache_state,
            "document_size_bytes": document_size,
            "result_count": executed_rows,
            "repetition": repetition,
            **metrics.as_dict(),
            "n_returned": explain_summary.get("n_returned"),
            "keys_examined": explain_summary.get("keys_examined"),
            "docs_examined": explain_summary.get("docs_examined"),
            "keys_examined_per_returned": explain_summary.get("keys_examined_per_returned"),
            "docs_examined_per_returned": explain_summary.get("docs_examined_per_returned"),
            "explain_branches": explain_summary.get("branches"),
            "explain_sort": explain_summary.get("sort"),
            "resource_metrics": resources,
            "pool_max_size": record["pool"]["max_size"],
            "pool_peak_in_use": pool_stats.get("peak_in_use"),
            "pool_wait_p95_ms": pool_stats.get("wait_p95_ms"),
            "pool_connections_created": pool_stats.get("connections_created"),
            "parallel_steps": parallel,
            "client_cpu_pct": round(client_cpu_pct, 2),
            "rtt_ms": self._rtt_ms,
            "warmup": cache_state == HOT,
        }
        for key, value in cell.extras.items():
            observation.setdefault(key, value)
        explain_record = {
            "model_id": model_name,
            "cell": cell.as_dict(),
            "repetition": repetition,
            "rendered": rendered.as_dict() if rendered is not None else None,
            **explain_summary,
        }
        return observation, explain_record

    def _ensure_recipe(
        self,
        session: MongoSession,
        manifest: CreationManifest,
        cells: list[ExperimentCell],
    ) -> None:
        if self._recipe_state is not None or self.dry_run:
            return
        dataset = self.resolved.dataset
        if dataset.recipe is None:
            return
        recipe = dataset.recipe
        scales = {int(size): name for size, name in dataset.scales.items()}
        wanted_docs = {cell.documents for cell in cells}
        if wanted_docs:
            scales = {size: name for size, name in scales.items() if size in wanted_docs}
        wanted_matches = sorted(
            {
                int(cell.extras["matches_per_key"])
                for cell in cells
                if "matches_per_key" in cell.extras
            }
        )
        if wanted_matches:
            recipe = recipe.model_copy(update={"matches_per_key": wanted_matches})
        console.print("[cyan]materializing data recipe[/cyan]")
        self._recipe_state = materialize_recipe(
            session.client,
            recipe,
            scales,
            safety=self.resolved.environment.safety,
            manifest=manifest,
            batch_size=self.resolved.execution.insert_batch_size,
            override_storage=self.override_storage,
        )
        console.print(
            f"recipe {self._recipe_state.fingerprint}: "
            f"created {len(self._recipe_state.created)}, reused {len(self._recipe_state.reused)}"
        )

    def _variables_for_cell(self, cell: ExperimentCell, database_name: str) -> dict[str, Any]:
        variables = cell.variables()
        recipe = self.resolved.dataset.recipe
        if recipe is None or self._recipe_state is None:
            variables.setdefault("collections", [])
            return variables
        matches = cell.extras.get("matches_per_key", recipe.matches_per_key[0])
        names = self._recipe_state.collections.get((database_name, int(matches)), [])
        if cell.extras.get("union_count") and int(cell.extras["union_count"]) > len(names):
            raise ExecutionError(
                f"union_count {cell.extras['union_count']} exceeds recipe copies ({len(names)})"
            )
        variables["collections"] = names
        variables["lead"] = names[0] if names else None
        variables["matches_per_key"] = int(matches)
        return variables

    def _seed_parameters(self, param_gen: QueryParameterGenerator, parameters: dict, database_name: str, cell: ExperimentCell) -> None:
        if self._recipe_state is None or self.resolved.dataset.recipe is None:
            return
        matches = cell.extras.get("matches_per_key", self.resolved.dataset.recipe.matches_per_key[0])
        keys = self._recipe_state.keys.get((database_name, int(matches))) or []
        if not keys:
            return
        for name, spec in parameters.items():
            param_gen.cached_values[spec.field or name] = list(keys)
            param_gen.cached_values[name] = list(keys)


def _same_point(row: dict[str, Any], model_name: str, cell: ExperimentCell) -> bool:
    if row.get("model_id") != model_name:
        return False
    if row.get("dataset_size") != cell.documents:
        return False
    if row.get("selectivity") != cell.selectivity:
        return False
    if row.get("concurrency") != cell.concurrency:
        return False
    if row.get("cache_state") != cell.cache_state:
        return False
    return all(row.get(key) == value for key, value in cell.extras.items())


def _sample_summary(counts: dict[str, dict[str, int]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, values in counts.items():
        ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
        summary[name] = {
            "distinct": len(values),
            "samples": int(sum(values.values())),
            "top": [{"value": key, "count": count} for key, count in ordered[:8]],
        }
    return summary
