"""Persist experiment runs as self-contained directories."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd

from perf_envelope import __version__


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{stamp}_{uuid4().hex[:6]}"


def git_commit() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
            ).strip()
        )
    except Exception:  # noqa: BLE001
        return "unknown"


class ExperimentRepository:
    def __init__(self, runs_dir: str | Path = "runs"):
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def create(self, run_id: str | None = None) -> Path:
        run_id = run_id or new_run_id()
        path = self.runs_dir / run_id
        (path / "configuration").mkdir(parents=True, exist_ok=True)
        (path / "model").mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, run_dir: Path, relative: str, payload: Any) -> Path:
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str))
        return path

    def read_json(self, run_dir: Path, relative: str) -> Any:
        return json.loads((run_dir / relative).read_text())

    def save_observations(self, run_dir: Path, rows: list[dict[str, Any]]) -> Path:
        cleaned = []
        for row in rows:
            item = dict(row)
            for key, value in list(item.items()):
                if isinstance(value, (dict, list)):
                    item[key] = json.dumps(value, default=str)
            cleaned.append(item)
        frame = pd.DataFrame(cleaned)
        path = run_dir / "observations.parquet"
        frame.to_parquet(path, index=False)
        csv_path = run_dir / "observations.csv"
        frame.to_csv(csv_path, index=False)
        return path

    def load_observations(self, run_dir: Path) -> pd.DataFrame:
        parquet = run_dir / "observations.parquet"
        if parquet.exists():
            return pd.read_parquet(parquet)
        csv_path = run_dir / "observations.csv"
        if csv_path.exists():
            return pd.read_csv(csv_path)
        raise FileNotFoundError(f"No observations in {run_dir}")

    def provenance(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "application_version": __version__,
            "git_commit": git_commit(),
            "timestamp": datetime.now(UTC).isoformat(),
            "python": os.sys.version,
        }
        if extra:
            payload.update(extra)
        return payload

    def latest(self) -> Path | None:
        runs = [p for p in self.runs_dir.iterdir() if p.is_dir() and p.name.startswith("run_")]
        if not runs:
            return None
        return max(runs, key=lambda p: p.stat().st_mtime)

    def resolve(self, run_id: str | None) -> Path:
        if run_id:
            path = Path(run_id)
            if path.exists() and path.is_dir():
                return path
            candidate = self.runs_dir / run_id
            if candidate.exists():
                return candidate
            raise FileNotFoundError(f"Run not found: {run_id}")
        latest = self.latest()
        if latest is None:
            raise FileNotFoundError("No runs found")
        return latest
