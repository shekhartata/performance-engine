from perf_envelope.diagnostics.heuristics import collect_diagnostics
from perf_envelope.environment.mongodb import redact_uri
from perf_envelope.environment.safety import (
    assert_non_production,
    is_managed_collection,
    managed_collection_name,
)
from perf_envelope.config.models import SafetyConfig
from perf_envelope.exceptions import SafetyError
import pandas as pd


def test_redact_uri_password():
    uri = "mongodb+srv://user:s3cret@cluster.mongodb.net/db"
    redacted = redact_uri(uri)
    assert "s3cret" not in redacted
    assert "***" in redacted


def test_managed_collection_prefix():
    name = managed_collection_name("getting-started", "main")
    assert name.startswith("perfenv_")
    assert is_managed_collection(name)
    assert not is_managed_collection("users")


def test_non_production_gate():
    try:
        assert_non_production(SafetyConfig(non_production=True), acknowledged=False)
        assert False
    except SafetyError:
        pass
    assert_non_production(SafetyConfig(non_production=True), acknowledged=True)


def test_diagnostics_scan_amplification():
    frame = pd.DataFrame(
        {
            "dataset_size": [100, 1000],
            "docs_examined_per_returned": [1.0, 40.0],
            "keys_examined_per_returned": [1.0, 20.0],
            "cache_state": ["estimated_hot", "estimated_cold"],
            "p95_ms": [10.0, 40.0],
            "concurrency": [1, 32],
        }
    )
    diags = collect_diagnostics(frame)
    hypotheses = {d["hypothesis"] for d in diags}
    assert "query scan amplification" in hypotheses
    assert "index scan amplification" in hypotheses
    assert "cache/storage sensitivity" in hypotheses
    assert "concurrency/resource saturation" in hypotheses
