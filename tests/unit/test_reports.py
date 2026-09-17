from pathlib import Path

import pandas as pd

from perf_envelope.analysis.pipeline import analyze_frame
from perf_envelope.comparison.comparator import compare_models
from perf_envelope.reports.json import build_report, generate_reports
from perf_envelope.storage.runs import ExperimentRepository


def test_compare_and_report(tmp_path: Path):
    frame = pd.DataFrame(
        {
            "model_id": ["embedded"] * 3 + ["referenced"] * 3,
            "dataset_size": [100, 1000, 10000, 100, 1000, 10000],
            "selectivity": [0.01] * 6,
            "concurrency": [1] * 6,
            "cache_state": ["estimated_hot"] * 6,
            "p95_ms": [8, 20, 90, 9, 28, 140],
            "p50_ms": [5, 12, 40, 6, 15, 70],
            "p99_ms": [12, 30, 120, 14, 40, 180],
            "qps": [100] * 6,
            "error_rate": [0.0] * 6,
            "document_size_bytes": [512] * 6,
            "result_count": [100] * 6,
        }
    )
    analysis = analyze_frame(frame, slo_p95=100)
    comparison = compare_models(frame, 100, analysis)
    assert set(comparison["models"]) == {"embedded", "referenced"}
    assert comparison["summary"]["referenced"]["first_slo_violation"] == 10000

    repo = ExperimentRepository(tmp_path)
    run_dir = repo.create("run_test")
    repo.save_observations(run_dir, frame.to_dict(orient="records"))
    repo.write_json(run_dir, "analysis.json", analysis)
    repo.write_json(run_dir, "configuration/slo.json", {"latency": {"p95_ms": 100}})
    report = build_report(run_dir, frame, analysis, slo={"latency": {"p95_ms": 100}})
    assert report["confidence"] in {"HIGH", "MEDIUM", "LOW"}
    assert "p95_vs_dataset_size" in report["plots"]
    assert "curves_by_scale" in report["plots"]
    assert report["scale_curves"]
    paths = generate_reports(run_dir, repo)
    assert paths["html"].exists()
    assert paths["markdown"].exists()
    assert "Executive Summary" in paths["markdown"].read_text()
