"""Local FastAPI app: connect, pick a collection, run an envelope test."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from perf_envelope.analysis.pipeline import analyze_run
from perf_envelope.api.spec_builder import AdvancedOptions, RunRequest, build_spec, with_second_model
from perf_envelope.api.state import COOKIE, LOCK, RUNS, SESSIONS, RunRecord, SessionRecord
from perf_envelope.config.spec import compile_spec_data
from perf_envelope.environment.mongodb import (
    list_collections,
    list_user_databases,
    normalize_uri,
    open_client,
    redact_uri,
)
from perf_envelope.exceptions import PerfEnvelopeError
from perf_envelope.experiment.runner import ExperimentRunner
from perf_envelope.reports.json import generate_reports
from perf_envelope.storage.runs import ExperimentRepository

log = logging.getLogger("perf_envelope.api")
load_dotenv()
router = APIRouter(prefix="/api")


class ConnectBody(BaseModel):
    uri: str = Field(min_length=1)


def _web_dist() -> Path | None:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "web" / "dist",
        here.parent / "static",
    ]
    for path in candidates:
        if (path / "index.html").is_file():
            return path
    return None


def current_session(request: Request) -> SessionRecord:
    sid = request.cookies.get(COOKIE)
    if not sid:
        raise HTTPException(status_code=401, detail="Connect to MongoDB first")
    with LOCK:
        session = SESSIONS.get(sid)
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired. Connect again.")
    return session


def _owned_run(run_id: str, session: SessionRecord) -> RunRecord:
    with LOCK:
        record = RUNS.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if record.session_id != session.id:
        raise HTTPException(status_code=404, detail="Run not found")
    return record


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/session")
def create_session(body: ConnectBody, response: Response) -> dict[str, Any]:
    uri = normalize_uri(body.uri)
    if not uri:
        raise HTTPException(status_code=400, detail="URI is required")
    try:
        client = open_client(uri, server_selection_timeout_ms=20_000)
        try:
            databases = list_user_databases(client)
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("MongoDB connect failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=400,
            detail="Could not connect. Check the URI and that the cluster is reachable.",
        ) from None
    sid = uuid4().hex
    record = SessionRecord(id=sid, uri=uri, uri_redacted=redact_uri(uri))
    with LOCK:
        SESSIONS[sid] = record
    response.set_cookie(COOKIE, sid, httponly=True, samesite="lax", max_age=8 * 3600)
    return {"id": sid, "uri_redacted": record.uri_redacted, "databases": databases}


@router.get("/session")
def read_session(session: SessionRecord = Depends(current_session)) -> dict[str, Any]:
    client = open_client(session.uri)
    try:
        databases = list_user_databases(client)
    finally:
        client.close()
    return {"id": session.id, "uri_redacted": session.uri_redacted, "databases": databases}


@router.delete("/session")
def delete_session(response: Response, request: Request) -> dict[str, str]:
    sid = request.cookies.get(COOKIE)
    if sid:
        with LOCK:
            SESSIONS.pop(sid, None)
    response.delete_cookie(COOKIE)
    return {"status": "disconnected"}


@router.get("/databases/{database}/collections")
def read_collections(
    database: str, session: SessionRecord = Depends(current_session)
) -> dict[str, Any]:
    client = open_client(session.uri)
    try:
        names = list_collections(client, database)
    finally:
        client.close()
    return {"database": database, "collections": names}


@router.post("/runs")
def start_run(body: RunRequest, session: SessionRecord = Depends(current_session)) -> dict[str, Any]:
    if not body.ack_non_production:
        raise HTTPException(
            status_code=400,
            detail="Acknowledge that this is a non-production cluster before running.",
        )
    if not body.database or not body.collection:
        raise HTTPException(status_code=400, detail="Database and collection are required")
    with LOCK:
        busy = any(
            item.session_id == session.id and item.status in {"queued", "running"}
            for item in RUNS.values()
        )
    if busy:
        raise HTTPException(status_code=409, detail="A run is already in progress")
    run_id = f"run_ui_{uuid4().hex[:10]}"
    record = RunRecord(
        id=run_id,
        session_id=session.id,
        database=body.database,
        collection=body.collection,
        status="queued",
        progress={"phase": "queued"},
    )
    with LOCK:
        RUNS[run_id] = record
    thread = Thread(target=_execute_run, args=(run_id, session, body), daemon=True)
    thread.start()
    return serialize_run(record)


@router.get("/runs/{run_id}")
def read_run(run_id: str, session: SessionRecord = Depends(current_session)) -> dict[str, Any]:
    return serialize_run(_owned_run(run_id, session))


@router.get("/runs/{run_id}/report")
def read_report(
    run_id: str,
    format: str = "html",
    session: SessionRecord = Depends(current_session),
) -> FileResponse:
    record = _owned_run(run_id, session)
    if record.status != "complete" or not record.run_dir:
        raise HTTPException(status_code=409, detail="Report is not ready")
    suffix = {"html": "html", "json": "json", "md": "md", "markdown": "md"}.get(format)
    if not suffix:
        raise HTTPException(status_code=400, detail="format must be html, json, or md")
    path = Path(record.run_dir) / f"report.{suffix}"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report file is missing")
    media = {"html": "text/html", "json": "application/json", "md": "text/markdown"}[suffix]
    return FileResponse(path, media_type=media, filename=path.name)


def serialize_run(record: RunRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "status": record.status,
        "database": record.database,
        "collection": record.collection,
        "progress": record.progress,
        "error": record.error,
        "analysis": record.analysis,
        "observations": record.observations,
    }


def summarize_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    per_model = analysis.get("per_model") or {}
    first = next(iter(per_model.values()), {})
    envelope = first.get("envelope") or []
    classes = [row.get("class") for row in envelope]
    worst = "GREEN"
    if "AMBER" in classes:
        worst = "AMBER"
    if "RED" in classes:
        worst = "RED"
    return {
        "worst_class": worst,
        "envelope": envelope,
        "per_scale": first.get("per_scale") or analysis.get("per_scale"),
        "slo_boundary": first.get("slo_boundary"),
        "projection": first.get("projection") or analysis.get("projection"),
        "validation": analysis.get("validation"),
        "diagnostics": analysis.get("diagnostics"),
        "models": list(per_model.keys()),
        "per_model": {
            name: {
                "envelope": payload.get("envelope"),
                "per_scale": payload.get("per_scale"),
                "slo_boundary": payload.get("slo_boundary"),
                "projection": payload.get("projection"),
            }
            for name, payload in per_model.items()
        },
    }


def _execute_run(run_id: str, session: SessionRecord, body: RunRequest) -> None:
    advanced = body.advanced or AdvancedOptions()

    def progress(payload: dict[str, Any]) -> None:
        with LOCK:
            if run_id in RUNS:
                RUNS[run_id].progress = payload
                RUNS[run_id].status = "running"

    try:
        progress({"phase": "compile"})
        spec = build_spec(
            uri=session.uri,
            database=body.database,
            collection=body.collection,
            query=body.query,
            advanced=advanced,
        )
        resolved = compile_spec_data(spec, source=Path("ui-run.yaml"))
        resolved = with_second_model(
            resolved, advanced.second_query, body.collection, advanced.second_model
        )
        progress({"phase": "run"})
        repo = ExperimentRepository(resolved.execution.runs_dir)
        runner = ExperimentRunner(
            resolved,
            acknowledged=True,
            allow_external_writes=advanced.allow_external_writes,
            repo=repo,
            on_progress=progress,
        )
        runner.run(run_id)
        progress({"phase": "analyze"})
        run_dir = repo.resolve(run_id)
        analyze_run(run_dir, advanced.slo_p95, 0.7, repo)
        try:
            generate_reports(run_dir, repo)
        except Exception as exc:  # noqa: BLE001
            log.warning("Report generation failed: %s", exc)
        frame = repo.load_observations(run_dir)
        analysis = repo.read_json(run_dir, "analysis.json")
        observations = json.loads(frame.to_json(orient="records"))
        with LOCK:
            RUNS[run_id].status = "complete"
            RUNS[run_id].progress = {"phase": "complete"}
            RUNS[run_id].run_dir = str(run_dir)
            RUNS[run_id].analysis = summarize_analysis(analysis)
            RUNS[run_id].observations = observations
    except Exception as exc:  # noqa: BLE001
        log.warning("UI run %s failed: %s", run_id, exc)
        with LOCK:
            if run_id in RUNS:
                RUNS[run_id].status = "failed"
                RUNS[run_id].error = str(exc)
                RUNS[run_id].progress = {"phase": "failed"}


def create_app() -> FastAPI:
    app = FastAPI(title="Performance Envelope Engine", docs_url="/api/docs", redoc_url=None)

    @app.exception_handler(PerfEnvelopeError)
    async def _pee_error(_request: Request, exc: PerfEnvelopeError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8000",
            "http://localhost:8000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    dist = _web_dist()
    if dist is not None:
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")
    return app


app = create_app()
