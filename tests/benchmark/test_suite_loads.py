from pathlib import Path

from perf_envelope.config.loader import ProjectLoader

ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS = ROOT / "benchmarks"


def test_benchmark_projects_validate():
    for path in sorted(BENCHMARKS.iterdir()):
        if not path.is_dir() or path.name.startswith("_"):
            continue
        if not (path / "project.yaml").exists():
            continue
        loader = ProjectLoader(path)
        resolved = loader.resolve_experiment("scale")
        assert resolved.query.collection
        assert resolved.slo.latency.p95_ms == 100
        cache = (resolved.experiment.dimensions.cache_state.values if resolved.experiment.dimensions.cache_state else [])
        assert "hot" in cache and "cold" in cache
