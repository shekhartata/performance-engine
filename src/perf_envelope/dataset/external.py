"""Shell out to an external data-generator repository at each scale."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from string import Formatter
from typing import Any

from perf_envelope.config.models import DatasetConfig, ExternalGeneratorConfig
from perf_envelope.environment.mongodb import redact_uri
from perf_envelope.environment.safety import CreationManifest
from perf_envelope.exceptions import ConfigError, ExecutionError

COMMAND_PLACEHOLDERS = frozenset({"documents", "uri", "database", "collection", "seed", "model"})
COLLECTION_PLACEHOLDERS = frozenset({"collection", "documents", "model"})


def placeholders(template: str) -> set[str]:
    names: set[str] = set()
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name:
            names.add(field_name.split("!")[0].split(":")[0])
    return names


def render_command(template: str, **values: Any) -> str:
    used = placeholders(template)
    unknown = used - COMMAND_PLACEHOLDERS
    if unknown:
        raise ConfigError(
            f"Unknown generator placeholders: {sorted(unknown)}. "
            f"Allowed: {sorted(COMMAND_PLACEHOLDERS)}"
        )
    missing = used - set(values)
    if missing:
        raise ConfigError(f"Missing generator placeholder values: {sorted(missing)}")
    payload = {key: values[key] for key in used}
    return template.format(**payload)


def render_collection_name(template: str, **values: Any) -> str:
    used = placeholders(template)
    unknown = used - COLLECTION_PLACEHOLDERS
    if unknown:
        raise ConfigError(
            f"Unknown collection-template placeholders: {sorted(unknown)}. "
            f"Allowed: {sorted(COLLECTION_PLACEHOLDERS)}"
        )
    missing = used - set(values)
    if missing:
        raise ConfigError(f"Missing collection-template values: {sorted(missing)}")
    payload = {key: values[key] for key in used}
    return template.format(**payload)


def count_within_tolerance(actual: int, expected: int, tolerance: float) -> bool:
    if expected <= 0:
        return actual == expected
    return abs(actual - expected) / expected <= tolerance


def collection_count(database, name: str) -> int:
    names = database.list_collection_names()
    if name not in names:
        return 0
    return int(database[name].estimated_document_count())


def materialize(
    session,
    dataset: DatasetConfig,
    *,
    documents: int,
    collection: str,
    database: str,
    uri: str,
    seed: int,
    model: str = "default",
    log_dir: Path | None = None,
    working_dir: Path | None = None,
    manifest: CreationManifest | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the configured generator (unless the scale already exists) and verify count."""
    generator = dataset.generator
    if generator is None:
        raise ConfigError("dataset.mode 'external' requires a generator block")
    actual = collection_count(session.database, collection)
    reused = False
    if generator.reuse_if_present and count_within_tolerance(
        actual, documents, generator.count_tolerance
    ):
        reused = True
    elif dry_run:
        pass
    else:
        _run_generator(
            generator,
            documents=documents,
            collection=collection,
            database=database,
            uri=uri,
            seed=seed,
            model=model,
            log_dir=log_dir,
            working_dir=working_dir,
        )
        actual = collection_count(session.database, collection)
        if not count_within_tolerance(actual, documents, generator.count_tolerance):
            raise ExecutionError(
                f"External generator produced {actual} documents in '{collection}', "
                f"expected {documents} ± {generator.count_tolerance:.0%}"
            )
    if manifest:
        manifest.add(
            "external_collection",
            collection,
            documents=actual,
            expected=documents,
            reused=reused,
        )
    return {"collection": collection, "documents": actual, "reused": reused}


def _run_generator(
    generator: ExternalGeneratorConfig,
    *,
    documents: int,
    collection: str,
    database: str,
    uri: str,
    seed: int,
    model: str,
    log_dir: Path | None,
    working_dir: Path | None,
) -> None:
    rendered = render_command(
        generator.command,
        documents=documents,
        uri=uri,
        database=database,
        collection=collection,
        seed=seed,
        model=model,
    )
    argv = shlex.split(rendered)
    cwd = None
    if working_dir is not None:
        cwd = str(working_dir)
    elif generator.working_dir:
        cwd = generator.working_dir
    env = os.environ.copy()
    env.update(generator.env)
    redacted = redact_uri(rendered)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=generator.timeout_seconds,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _write_log(log_dir, documents, redacted, "", str(exc))
        raise ExecutionError(
            f"External generator timed out after {generator.timeout_seconds}s: {redacted}"
        ) from exc
    _write_log(log_dir, documents, redacted, completed.stdout, completed.stderr)
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise ExecutionError(
            f"External generator exited {completed.returncode}: {redacted}\n{tail}"
        )


def _write_log(
    log_dir: Path | None,
    documents: int,
    command: str,
    stdout: str,
    stderr: str,
) -> None:
    if log_dir is None:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{documents}.log"
    path.write_text(
        f"$ {command}\n\n--- stdout ---\n{stdout or ''}\n--- stderr ---\n{stderr or ''}\n"
    )
