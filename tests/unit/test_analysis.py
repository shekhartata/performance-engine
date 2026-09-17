from pathlib import Path

import pandas as pd

from perf_envelope.analysis.features import FEATURE_COLUMNS, transform
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


def test_feature_transform_columns():
    frame = _synthetic_frame()
    out = transform(frame)
    for col in FEATURE_COLUMNS:
        assert col in out.columns


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
