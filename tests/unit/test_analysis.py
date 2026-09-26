from pathlib import Path

import pandas as pd

import itertools

import numpy as np

from perf_envelope.analysis.features import build_feature_spec, feature_matrix
from perf_envelope.analysis.pipeline import analyze_frame
from perf_envelope.frontier.change_point import detect_change_points
from perf_envelope.frontier.envelope import build_envelope
from perf_envelope.frontier.slo_boundary import detect_slo_boundary


FIXTURE = Path(__file__).resolve().parents[1] / "synthetic" / "slo_boundary.csv"


def _synthetic_frame() -> pd.DataFrame:
    csv = pd.read_csv(FIXTURE)
    csv = csv.rename(columns={"N": "dataset_size", "p95": "p95_ms"})
    csv["model_id"] = "referenced"
    csv["cache_state"] = "estimated_hot"
    csv["document_size_bytes"] = 512
    csv["result_count"] = 100
    csv["n_returned"] = 100
    csv["keys_examined"] = 110
    csv["docs_examined"] = 100
    csv["keys_examined_per_returned"] = 1.1
    csv["docs_examined_per_returned"] = 1.0
    return csv


def test_features_use_only_varied_settings():
    frame = _synthetic_frame()
    spec = build_feature_spec(frame)
    _, _, names = feature_matrix(frame, spec=spec)
    assert names == ["log_dataset_size"]
    assert "log_result_cardinality" not in names
    assert spec.held_fixed["cache_state"] == "estimated_hot"
    assert spec.held_fixed["document_size_bytes"] == 512


def _union_grid(repeats: int = 1) -> pd.DataFrame:
    rows = []
    for plan, n, k, c, rep in itertools.product(
        ["server", "app"], [1, 2, 4, 8], [1, 50], [1, 8], range(repeats)
    ):
        base = 20 * (1 + 0.3 * (n - 1) * (plan == "server")) * (1 + 0.02 * n * k / 8) * (1 + 0.5 * (c == 8))
        rows.append(
            {
                "model_id": plan,
                "plan": plan,
                "dataset_size": 100_000,
                "selectivity": 0.5,
                "concurrency": c,
                "cache_state": "estimated_hot",
                "union_count": n,
                "matches_per_key": k,
                "result_count": n * k,
                "p95_ms": base * (1 + 0.01 * rep),
            }
        )
    return pd.DataFrame(rows)


def test_attribution_splits_across_sweep_settings_not_result_count():
    analysis = analyze_frame(_union_grid(), slo_p95=100, sweep_axes=["union_count", "matches_per_key"])
    attribution = analysis["attribution"]
    assert attribution["method"] == "variance_split"
    assert set(attribution["settings"]) == {"Plan", "Concurrency", "Union Count", "Matches Per Key"}
    assert "Result Cardinality" not in attribution["settings"]
    assert attribution["settings"]["Union Count"] > 5
    assert any("×" in name for name in attribution["pairs"])
    assert attribution["held_fixed"]["Selectivity"] == 0.5
    measured = attribution["measured_outputs"][0]
    assert measured["name"] == "Documents per request"
    assert measured["determined_by_settings"] is True
    total = sum(attribution["settings"].values()) + sum(attribution["pairs"].values()) + attribution["higher_order"]
    assert abs(total - 100) < 1.0


def test_attribution_reports_noise_with_repetitions():
    analysis = analyze_frame(_union_grid(repeats=2), slo_p95=100, sweep_axes=["union_count", "matches_per_key"])
    assert analysis["attribution"]["noise"] is not None


def test_attribution_falls_back_to_permutation_on_incomplete_grid():
    frame = _union_grid().iloc[:-3]
    analysis = analyze_frame(frame, slo_p95=100, sweep_axes=["union_count", "matches_per_key"])
    attribution = analysis["attribution"]
    assert attribution["method"] == "permutation"
    assert attribution["pairs"] == {}
    assert abs(sum(attribution["settings"].values()) - 100) < 1.0


def test_prediction_fills_settings_missing_from_the_frame():
    frame = _union_grid()
    analysis = analyze_frame(
        frame, slo_p95=100, sweep_axes=["union_count", "matches_per_key"], project_documents=[200_000]
    )
    projected = analysis["projection"][0]
    assert projected["extrapolated"] is True
    assert np.isfinite(projected["predicted_p95_ms"])


def test_slo_boundary_from_fixture():
    frame = _synthetic_frame()
    result = detect_slo_boundary(frame, slo_p95=100)
    assert result["kind"] == "OBSERVED"
    assert result["last_pass"]["dataset_size"] == 100_000_000
    assert result["first_fail"]["dataset_size"] == 200_000_000
    assert 100_000_000 < result["estimated_breakpoint_documents"] < 200_000_000


def test_change_point_flags_inflection():
    frame = _synthetic_frame()
    points = detect_change_points(frame, multiplier=2.0)
    assert points
    assert points[-1]["to"] == 200_000_000


def test_envelope_classes():
    frame = _synthetic_frame()
    envelope = build_envelope(frame, slo_p95=100)
    classes = {row["dataset_size"]: row["class"] for row in envelope}
    assert classes[100_000] == "GREEN"
    assert classes[200_000_000] == "RED"


def test_analyze_pipeline_on_fixture():
    frame = _synthetic_frame()
    analysis = analyze_frame(frame, slo_p95=100)
    assert analysis["row_count"] == 6
    assert "mape" in analysis["validation"]
    boundary = analysis["per_model"]["referenced"]["slo_boundary"]
    assert boundary["kind"] == "OBSERVED"
    assert analysis["predictions"]
