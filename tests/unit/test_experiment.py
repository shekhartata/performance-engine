from perf_envelope.config.models import ExperimentDimensions
from perf_envelope.experiment.matrix import build_matrix
from perf_envelope.experiment.refinement import classify, propose_refinement
from perf_envelope.telemetry.latency import summarize_latencies
from perf_envelope.workload.cache import HOT, normalize_cache_state


def test_latency_percentiles_ordered():
    summary = summarize_latencies([1, 2, 3, 4, 5, 100])
    assert summary["min_ms"] == 1
    assert summary["max_ms"] == 100
    assert summary["p50_ms"] <= summary["p95_ms"] <= summary["p99_ms"]


def test_matrix_cartesian():
    dims = ExperimentDimensions.model_validate(
        {
            "documents": {"values": [100, 200]},
            "selectivity": {"values": [0.01]},
            "concurrency": {"values": [1, 8]},
            "cache_state": {"values": ["hot", "cold"]},
        }
    )
    cells = build_matrix(dims)
    assert len(cells) == 8
    assert {c.cache_state for c in cells} == {HOT, "estimated_cold"}


def test_cache_normalization():
    assert normalize_cache_state("hot") == "estimated_hot"
    assert normalize_cache_state("COLD") == "estimated_cold"


def test_refinement_inserts_midpoint():
    dims = ExperimentDimensions.model_validate(
        {
            "documents": {"values": [10_000_000, 100_000_000]},
            "selectivity": {"values": [0.01]},
            "concurrency": {"values": [1]},
            "cache_state": {"values": ["hot"]},
        }
    )
    cells = build_matrix(dims)
    p95 = {cells[0].key(): 20.0, cells[1].key(): 150.0}
    extra = propose_refinement(cells, p95, slo_p95=100, uncertainty=0.15)
    assert extra
    assert extra[0].documents == 55_000_000


def test_classify_envelope():
    assert classify(50, 100) == "GREEN"
    assert classify(80, 100) == "AMBER"
    assert classify(120, 100) == "RED"
