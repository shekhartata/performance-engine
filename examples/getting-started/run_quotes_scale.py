"""Ensure 50K/100K quote datasets exist, run the lookup envelope, merge curves."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SPEC = Path(__file__).resolve().parent / "quotes_lookup.yaml"
GENERATOR = Path("/Users/chandrashekhartata/radianDataGenerator")
GEN_PYTHON = GENERATOR / ".venv" / "bin" / "python"

# 10K is already loaded. Larger scales go to dedicated databases so we never
# drop the existing quotes collection.
SCALES = {
    10_000: "mi_transformation",
    50_000: "mi_quotes_50000",
    100_000: "mi_quotes_100000",
}


def uri() -> str:
    load_dotenv(ROOT / ".env")
    value = os.environ.get("MONGODB_URI")
    if not value:
        raise SystemExit("MONGODB_URI is not set")
    return value


def quotes_count(database: str) -> int:
    from pymongo import MongoClient

    client = MongoClient(uri(), serverSelectionTimeoutMS=15000)
    try:
        names = client[database].list_collection_names()
        if "quotes" not in names:
            return 0
        return int(client[database]["quotes"].estimated_document_count())
    finally:
        client.close()


def ensure_scale(documents: int, database: str) -> None:
    actual = quotes_count(database)
    if actual >= int(documents * 0.95):
        print(f"reuse {database}.quotes ({actual} docs)")
        return
    if documents == 10_000:
        raise SystemExit(
            f"{database}.quotes has {actual} documents; expected the existing 10K load"
        )
    print(f"generating {documents} quotes into {database}")
    cmd = [
        str(GEN_PYTHON),
        "-m",
        "midatagen",
        "--uri",
        uri(),
        "--db",
        database,
        "--quotes",
        str(documents),
        "--seed",
        "42",
        "--payload-mode",
        "skip",
        "--drop",
        "--yes",
        "--report-path",
        str(GENERATOR / f"report_{database}.json"),
    ]
    subprocess.run(cmd, cwd=str(GENERATOR), check=True)
    print(f"generated {database}.quotes = {quotes_count(database)}")


def run_scale(documents: int, database: str) -> str:
    from perf_envelope.config.spec import compile_spec
    from perf_envelope.experiment.runner import ExperimentRunner

    resolved = compile_spec(SPEC, database=database, collection="quotes")
    runner = ExperimentRunner(resolved, acknowledged=True)
    run_id = runner.run()
    print(f"{documents} docs ({database}): run {run_id}")
    return run_id


def merge_and_analyze(run_ids: list[str]) -> Path:
    import pandas as pd

    from perf_envelope.analysis.pipeline import analyze_frame
    from perf_envelope.reports.json import generate_reports
    from perf_envelope.storage.runs import ExperimentRepository

    repo = ExperimentRepository(ROOT / "runs")
    frames = []
    for run_id in run_ids:
        run_dir = repo.resolve(run_id)
        frame = repo.load_observations(run_dir)
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True)
    combined_dir = repo.create("run_quotes_lookup_combined")
    repo.save_observations(combined_dir, combined.to_dict(orient="records"))
    repo.write_json(
        combined_dir,
        "configuration/slo.json",
        {"latency": {"p95_ms": 100, "p99_ms": 250}, "green_fraction": 0.7},
    )
    repo.write_json(
        combined_dir,
        "configuration/experiment.json",
        {
            "id": "quotes_loan_lookup",
            "dimensions": {
                "documents": {"values": list(SCALES)},
                "concurrency": {"values": [1, 8]},
            },
        },
    )
    analysis = analyze_frame(combined, slo_p95=100, green_fraction=0.7)
    repo.write_json(combined_dir, "analysis.json", analysis)
    generate_reports(combined_dir, repo)
    print(f"combined report: {combined_dir}")
    return combined_dir


def main() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    os.chdir(ROOT)
    run_ids = []
    for documents, database in SCALES.items():
        ensure_scale(documents, database)
        run_ids.append(run_scale(documents, database))
    merge_and_analyze(run_ids)


if __name__ == "__main__":
    main()
