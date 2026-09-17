import pandas as pd

from perf_envelope.analysis.baseline import fit_baseline
from perf_envelope.analysis.pipeline import analyze_frame
from perf_envelope.analysis.projection import per_scale_curves, project_scales


def _frame() -> pd.DataFrame:
    rows = []
    for size, base in [(10_000, 8.0), (100_000, 40.0), (500_000, 90.0)]:
        for conc in (1, 8, 32):
            rows.append(
                {
                    "model_id": "default",
                    "dataset_size": size,
                    "selectivity": 0.01,
                    "concurrency": conc,
                    "cache_state": "estimated_hot",
                    "p95_ms": base * (1 + conc / 64),
                    "document_size_bytes": 512,
                    "result_count": 50,
                }
            )
    return pd.DataFrame(rows)


def test_per_scale_curves_are_measured():
    curves = per_scale_curves(_frame(), slo_p95=100)
    sizes = [row["dataset_size"] for row in curves]
    assert sizes == [10_000, 100_000, 500_000]
    assert all(row["extrapolated"] is False for row in curves)
    assert curves[0]["slo_pass"] is True
    assert curves[0]["p95_vs_concurrency"]


def test_unmeasured_scales_are_flagged_extrapolated():
    frame = _frame()
    baseline = fit_baseline(frame)
    projected = project_scales(frame, [10_000, 1_000_000], slo_p95=100, predictor=baseline)
    by_size = {row["dataset_size"]: row for row in projected}
    assert by_size[10_000]["extrapolated"] is False
    assert by_size[1_000_000]["extrapolated"] is True
    assert by_size[1_000_000]["measured_range"] == [10_000, 500_000]
    assert by_size[1_000_000]["predicted_p95_ms"] > 0


def test_analyze_frame_includes_per_scale_and_projection():
    analysis = analyze_frame(_frame(), slo_p95=100, project_documents=[1_000_000])
    assert analysis["per_scale"]
    assert analysis["projection"]
    assert analysis["projection"][0]["extrapolated"] is True
    assert analysis["per_model"]["default"]["per_scale"]
