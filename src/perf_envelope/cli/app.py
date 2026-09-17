"""Typer CLI. Business logic lives in the library modules, not here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from perf_envelope import __version__
from perf_envelope.comparison.comparator import compare_models
from perf_envelope.config.loader import ProjectLoader, ResolvedExperiment, list_yaml_stems
from perf_envelope.config.spec import compile_spec
from perf_envelope.dataset.generator import generate_into_mongo
from perf_envelope.dataset.guardrails import assert_within_limit, estimate_storage
from perf_envelope.dataset.verifier import verify_dataset
from perf_envelope.environment.discovery import discover
from perf_envelope.environment.mongodb import connect
from perf_envelope.environment.safety import (
    CreationManifest,
    assert_non_production,
    is_managed_collection,
    managed_collection_name,
)
from perf_envelope.exceptions import PerfEnvelopeError
from perf_envelope.experiment.planner import ExperimentPlanner
from perf_envelope.experiment.runner import ExperimentRunner
from perf_envelope.models.indexes import diff_indexes
from perf_envelope.query.canonicalizer import canonical_form, shape_id
from perf_envelope.storage.runs import ExperimentRepository

load_dotenv()

app = typer.Typer(no_args_is_help=True, help="Performance Envelope Engine")
dataset_app = typer.Typer(no_args_is_help=True, help="Synthetic dataset commands")
app.add_typer(dataset_app, name="dataset")
console = Console()


def _fail(exc: Exception) -> None:
    console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code=1)


DATABASE_OPTION = typer.Option(
    None, "--database", "-d", help="Override the target database (defaults to the environment file)"
)
COLLECTION_OPTION = typer.Option(
    None,
    "--collection",
    "-c",
    help="Point the suite at an existing collection (read-only, no dataset-size sweep)",
)
PROJECT_OPTION = typer.Option(
    None, "--project", "-p", exists=True, file_okay=False, help="Portable project directory"
)
SPEC_OPTION = typer.Option(
    None,
    "--spec",
    "-s",
    exists=True,
    dir_okay=False,
    help="Single-file target spec (database + collection + query)",
)
EXPERIMENT_OPTION = typer.Option(None, "--experiment", "-e", help="Experiment name under the project")


def _resolve(
    project: Path | None,
    spec: Path | None,
    experiment: str | None = None,
    *,
    database: str | None = None,
    collection: str | None = None,
    require_experiment: bool = False,
) -> ResolvedExperiment:
    if project and spec:
        raise PerfEnvelopeError("Pass either --project or --spec, not both")
    if not project and not spec:
        raise PerfEnvelopeError("Provide --project or --spec")
    if spec:
        return compile_spec(spec, database=database, collection=collection)
    loader = ProjectLoader(project)
    name = experiment
    if not name:
        names = list_yaml_stems(loader.project_dir, "experiment")
        if require_experiment or not names:
            raise PerfEnvelopeError("No experiment specified")
        name = names[0]
    return loader.resolve_experiment(name, database=database, collection=collection)


def _target_label(resolved: ResolvedExperiment) -> str:
    db = resolved.environment.connection.database
    mode = resolved.dataset.mode
    if mode == "existing":
        return f"{db}.{resolved.dataset.collection or resolved.query.collection} (existing)"
    if mode == "external":
        return f"{db} (external generator)"
    return f"{db} (synthetic)"


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
) -> None:
    if version:
        console.print(__version__)
        raise typer.Exit()


@app.command()
def validate(
    project: Optional[Path] = PROJECT_OPTION,
    spec: Optional[Path] = SPEC_OPTION,
    experiment: Optional[str] = EXPERIMENT_OPTION,
    database: Optional[str] = DATABASE_OPTION,
    collection: Optional[str] = COLLECTION_OPTION,
) -> None:
    """Validate all input definitions."""
    try:
        if spec:
            resolved = _resolve(project, spec, experiment, database=database, collection=collection)
            console.print(
                f"[green]OK[/green] {resolved.experiment.id} models={resolved.model_names} "
                f"query={resolved.query.id} target={_target_label(resolved)}"
            )
            return
        if not project:
            raise PerfEnvelopeError("Provide --project or --spec")
        loader = ProjectLoader(project)
        names = [experiment] if experiment else list_yaml_stems(loader.project_dir, "experiment")
        if not names:
            raise PerfEnvelopeError("No experiments found")
        for name in names:
            resolved = loader.resolve_experiment(name, database=database, collection=collection)
            target = _target_label(resolved)
            console.print(
                f"[green]OK[/green] {name} models={resolved.model_names} "
                f"query={resolved.query.id} target={target}"
            )
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@app.command()
def inspect(
    project: Optional[Path] = PROJECT_OPTION,
    spec: Optional[Path] = SPEC_OPTION,
    experiment: Optional[str] = EXPERIMENT_OPTION,
    database: Optional[str] = DATABASE_OPTION,
    collection: Optional[str] = COLLECTION_OPTION,
) -> None:
    """Inspect Atlas environment, schema and indexes."""
    try:
        resolved = _resolve(project, spec, experiment, database=database, collection=collection)
        session = connect(resolved.environment)
        try:
            meta = discover(session)
            console.print_json(data=meta)
            console.print(f"Query shape {shape_id(resolved.query)}")
            console.print(canonical_form(resolved.query))
            db = session.database
            if resolved.dataset.mode == "existing":
                target = resolved.dataset.collection or resolved.query.collection
                if target in db.list_collection_names():
                    count = db[target].estimated_document_count()
                    console.print(f"{db.name}.{target}: {count} documents (existing, read-only)")
                    for idx in db[target].list_indexes():
                        console.print(f"  index {idx.get('name')} keys={dict(idx.get('key', {}))}")
                else:
                    console.print(f"[red]{db.name}.{target} does not exist[/red]")
                return
            if resolved.dataset.mode == "external":
                for entry in ExperimentPlanner(resolved).per_scale_collections():
                    name = entry["collection"]
                    if name in db.list_collection_names():
                        count = db[name].estimated_document_count()
                        console.print(f"{db.name}.{name}: {count} documents")
                    else:
                        console.print(f"{db.name}.{name}: does not exist yet")
                return
            for model_name, index_cfg in resolved.indexes.items():
                for logical, specs in index_cfg.indexes.items():
                    physical = managed_collection_name(
                        f"{resolved.project.name}_{model_name}",
                        logical,
                        resolved.environment.safety.managed_prefix,
                    )
                    if physical in db.list_collection_names():
                        diff = diff_indexes(db[physical], specs)
                        console.print(
                            f"{physical}: matched={diff.matched} missing={[s.name for s in diff.missing]}"
                        )
                    else:
                        console.print(f"{physical}: does not exist yet")
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@dataset_app.command("generate")
def dataset_generate(
    project: Optional[Path] = PROJECT_OPTION,
    spec: Optional[Path] = SPEC_OPTION,
    experiment: Optional[str] = EXPERIMENT_OPTION,
    ack_non_production: bool = typer.Option(False, "--ack-non-production"),
    override_storage_limit: bool = typer.Option(False, "--override-storage-limit"),
    model: Optional[str] = typer.Option(None, "--model"),
    database: Optional[str] = DATABASE_OPTION,
) -> None:
    """Generate a synthetic dataset into managed Atlas collections."""
    try:
        resolved = _resolve(
            project, spec, experiment, database=database, require_experiment=not spec
        )
        if resolved.dataset.mode == "existing":
            raise PerfEnvelopeError(
                "This experiment targets an existing collection; generation is disabled. "
                "Run `perfenv run` to benchmark it directly."
            )
        if resolved.dataset.mode == "external":
            raise PerfEnvelopeError(
                "External datasets are materialized during `perfenv run` by the configured generator."
            )
        assert_non_production(resolved.environment.safety, ack_non_production)
        model_name = model or resolved.model_names[0]
        estimate = estimate_storage(resolved.dataset)
        console.print(estimate.as_dict())
        assert_within_limit(
            estimate, resolved.environment.safety, override=override_storage_limit
        )
        physical = {
            logical: managed_collection_name(
                f"{resolved.project.name}_{model_name}",
                logical,
                resolved.environment.safety.managed_prefix,
            )
            for logical in resolved.models[model_name].collections
        }
        session = connect(resolved.environment)
        manifest = CreationManifest(project=resolved.project.name)
        try:
            counts = generate_into_mongo(
                session.database,
                resolved.dataset,
                physical,
                batch_size=resolved.execution.insert_batch_size,
                manifest=manifest,
            )
            console.print(counts)
            path = Path(resolved.execution.runs_dir) / "last-manifest.json"
            manifest.save(path)
            console.print(f"Manifest: {path}")
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@dataset_app.command("verify")
def dataset_verify(
    project: Optional[Path] = PROJECT_OPTION,
    spec: Optional[Path] = SPEC_OPTION,
    experiment: Optional[str] = EXPERIMENT_OPTION,
    model: Optional[str] = typer.Option(None, "--model"),
    database: Optional[str] = DATABASE_OPTION,
) -> None:
    """Validate distributions and document counts."""
    try:
        resolved = _resolve(
            project, spec, experiment, database=database, require_experiment=not spec
        )
        if resolved.dataset.mode == "existing":
            raise PerfEnvelopeError(
                "Distribution verification applies to synthetic datasets only. "
                "Use `perfenv inspect` to look at an existing collection."
            )
        if resolved.dataset.mode == "external":
            raise PerfEnvelopeError(
                "Distribution verification applies to synthetic datasets only. "
                "Use `perfenv inspect` to look at generator-produced collections."
            )
        model_name = model or resolved.model_names[0]
        physical = {
            logical: managed_collection_name(
                f"{resolved.project.name}_{model_name}",
                logical,
                resolved.environment.safety.managed_prefix,
            )
            for logical in resolved.dataset.collections
        }
        session = connect(resolved.environment)
        try:
            report = verify_dataset(session.database, resolved.dataset, physical)
            console.print_json(data=report)
            if not report["ok"]:
                raise typer.Exit(code=1)
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@app.command()
def plan(
    project: Optional[Path] = PROJECT_OPTION,
    spec: Optional[Path] = SPEC_OPTION,
    experiment: Optional[str] = EXPERIMENT_OPTION,
    database: Optional[str] = DATABASE_OPTION,
    collection: Optional[str] = COLLECTION_OPTION,
) -> None:
    """Generate the coarse experiment matrix without running it."""
    try:
        resolved = _resolve(
            project,
            spec,
            experiment,
            database=database,
            collection=collection,
            require_experiment=not spec,
        )
        payload = ExperimentPlanner(resolved).plan_dict()
        console.print_json(data=payload)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@app.command()
def run(
    project: Optional[Path] = PROJECT_OPTION,
    spec: Optional[Path] = SPEC_OPTION,
    experiment: Optional[str] = EXPERIMENT_OPTION,
    ack_non_production: bool = typer.Option(False, "--ack-non-production"),
    override_storage_limit: bool = typer.Option(False, "--override-storage-limit"),
    allow_external_writes: bool = typer.Option(
        False,
        "--allow-external-writes",
        help="Allow the external generator to write collections outside the perfenv_ prefix",
    ),
    analyze_after: bool = typer.Option(False, "--analyze"),
    report_after: bool = typer.Option(False, "--report"),
    database: Optional[str] = DATABASE_OPTION,
    collection: Optional[str] = COLLECTION_OPTION,
) -> None:
    """Execute the experiment against Atlas."""
    try:
        resolved = _resolve(
            project,
            spec,
            experiment,
            database=database,
            collection=collection,
            require_experiment=not spec,
        )
        console.print(f"Target: {_target_label(resolved)}")
        runner = ExperimentRunner(
            resolved,
            acknowledged=ack_non_production,
            override_storage=override_storage_limit,
            allow_external_writes=allow_external_writes,
        )
        run_id = runner.run()
        repo = ExperimentRepository(resolved.execution.runs_dir)
        run_dir = repo.resolve(run_id)
        repo.write_json(run_dir, "configuration/slo.json", resolved.slo.model_dump())
        if analyze_after or report_after:
            from perf_envelope.analysis.pipeline import analyze_run

            analyze_run(run_dir, resolved.slo.latency.p95_ms, resolved.slo.green_fraction, repo)
        if report_after:
            from perf_envelope.reports.json import generate_reports

            paths = generate_reports(run_dir, repo)
            console.print({k: str(v) for k, v in paths.items()})
        console.print(run_id)
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@app.command()
def analyze(
    run_id: Optional[str] = typer.Option(None, "--run"),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
    slo_p95: float = typer.Option(100.0, "--slo-p95"),
) -> None:
    """Fit models and detect boundaries for a completed run."""
    try:
        repo = ExperimentRepository(runs_dir)
        run_dir = repo.resolve(run_id)
        slo_path = run_dir / "configuration" / "slo.json"
        if slo_path.exists():
            slo = json.loads(slo_path.read_text())
            slo_p95 = slo.get("latency", {}).get("p95_ms", slo_p95)
            green = slo.get("green_fraction", 0.7)
        else:
            green = 0.7
        from perf_envelope.analysis.pipeline import analyze_run

        analysis = analyze_run(run_dir, slo_p95, green, repo)
        console.print_json(data={k: analysis[k] for k in analysis if k != "predictions"})
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@app.command()
def report(
    run_id: Optional[str] = typer.Option(None, "--run"),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
) -> None:
    """Generate JSON, Markdown and HTML reports."""
    try:
        repo = ExperimentRepository(runs_dir)
        run_dir = repo.resolve(run_id)
        if not (run_dir / "analysis.json").exists():
            slo_path = run_dir / "configuration" / "slo.json"
            slo_p95 = 100.0
            green = 0.7
            if slo_path.exists():
                slo = json.loads(slo_path.read_text())
                slo_p95 = slo.get("latency", {}).get("p95_ms", slo_p95)
                green = slo.get("green_fraction", 0.7)
            from perf_envelope.analysis.pipeline import analyze_run

            analyze_run(run_dir, slo_p95, green, repo)
        from perf_envelope.reports.json import generate_reports

        paths = generate_reports(run_dir, repo)
        for kind, path in paths.items():
            console.print(f"{kind}: {path}")
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@app.command()
def compare(
    run_id: Optional[str] = typer.Option(None, "--run"),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
) -> None:
    """Compare candidate models in a run."""
    try:
        repo = ExperimentRepository(runs_dir)
        run_dir = repo.resolve(run_id)
        frame = repo.load_observations(run_dir)
        analysis = {}
        if (run_dir / "analysis.json").exists():
            analysis = repo.read_json(run_dir, "analysis.json")
        slo_p95 = 100.0
        slo_path = run_dir / "configuration" / "slo.json"
        if slo_path.exists():
            slo_p95 = json.loads(slo_path.read_text()).get("latency", {}).get("p95_ms", slo_p95)
        payload = compare_models(frame, slo_p95, analysis)
        table = Table(title="Model comparison")
        table.add_column("dataset_size")
        for model in payload["models"]:
            table.add_column(f"{model} p95")
        for row in payload["p95_by_size"]:
            table.add_row(
                str(row["dataset_size"]),
                *[
                    f"{row.get(f'{model}_p95_ms'):.1f}" if row.get(f"{model}_p95_ms") is not None else "-"
                    for model in payload["models"]
                ],
            )
        console.print(table)
        console.print_json(data=payload["summary"])
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


@app.command()
def cleanup(
    project: Optional[Path] = PROJECT_OPTION,
    spec: Optional[Path] = SPEC_OPTION,
    experiment: Optional[str] = EXPERIMENT_OPTION,
    ack_non_production: bool = typer.Option(False, "--ack-non-production"),
    manifest: Optional[Path] = typer.Option(None, "--manifest"),
) -> None:
    """Drop engine-managed collections listed in a creation manifest."""
    try:
        resolved = _resolve(project, spec, experiment)
        assert_non_production(resolved.environment.safety, ack_non_production)
        session = connect(resolved.environment)
        try:
            path = manifest or Path(resolved.execution.runs_dir) / "last-manifest.json"
            if not path.exists():
                console.print("No manifest found; listing managed collections")
                for coll in session.database.list_collection_names():
                    if is_managed_collection(coll) and sanitize_match(coll, resolved.project.name):
                        session.database[coll].drop()
                        console.print(f"dropped {coll}")
                return
            data = CreationManifest.load(path)
            for obj in data.objects:
                if obj.kind == "collection" and is_managed_collection(obj.name):
                    session.database[obj.name].drop()
                    console.print(f"dropped {obj.name}")
                elif obj.kind == "external_collection":
                    session.database[obj.name].drop()
                    console.print(f"dropped {obj.name}")
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        _fail(exc)


def sanitize_match(collection: str, project: str) -> bool:
    return project.replace("-", "_").lower() in collection.lower()


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Serve the local web UI (paste a URI, pick a collection, run a test)."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        _fail(RuntimeError("Install uvicorn to use the UI (pip install -e .)"))
    dist = Path(__file__).resolve().parents[3] / "web" / "dist"
    if not (dist / "index.html").exists():
        console.print(
            "[yellow]Frontend build not found.[/yellow] API is still available at "
            f"http://{host}:{port}/api\n"
            "From the repo: [bold]cd web && npm install && npm run build[/bold]\n"
            "Or run the Vite dev server: [bold]cd web && npm run dev[/bold]"
        )
    console.print(f"UI http://{host}:{port}")
    uvicorn.run("perf_envelope.api.app:app", host=host, port=port, reload=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
