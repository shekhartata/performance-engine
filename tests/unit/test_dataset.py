import numpy as np

from perf_envelope.config.models import DatasetConfig, FieldSpec, SafetyConfig
from perf_envelope.dataset.distributions import sample_field
from perf_envelope.dataset.generator import generate_all
from perf_envelope.dataset.guardrails import assert_within_limit, estimate_storage
from perf_envelope.dataset.scaler import scale_dataset
from perf_envelope.exceptions import GuardrailError


def test_zipf_respects_cardinality():
    rng = np.random.default_rng(0)
    spec = FieldSpec(type="integer", cardinality=10, distribution="zipf", alpha=1.2)
    values = sample_field(spec, 200, rng)
    assert set(values) <= set(range(1, 11))


def test_categorical_weights():
    rng = np.random.default_rng(1)
    spec = FieldSpec(type="categorical", values={"A": 1.0, "B": 0.0})
    values = sample_field(spec, 50, rng)
    assert set(values) == {"A"}


def test_generate_with_relationship():
    dataset = DatasetConfig.model_validate(
        {
            "mode": "synthetic",
            "seed": 7,
            "collections": {
                "customers": {
                    "count": 20,
                    "fields": {
                        "customer_id": {
                            "type": "integer",
                            "cardinality": 20,
                            "distribution": "sequential",
                        }
                    },
                },
                "orders": {
                    "count": 40,
                    "fields": {
                        "customer_id": {
                            "type": "integer",
                            "references": "customers.customer_id",
                            "distribution": "zipf",
                        }
                    },
                },
            },
        }
    )
    docs = generate_all(dataset)
    parent_ids = {d["customer_id"] for d in docs["customers"]}
    child_ids = {d["customer_id"] for d in docs["orders"]}
    assert child_ids <= parent_ids
    assert len(docs["orders"]) == 40


def test_scale_preserves_ratio():
    dataset = DatasetConfig.model_validate(
        {
            "mode": "synthetic",
            "collections": {
                "orders": {
                    "count": 1000,
                    "fields": {
                        "customer_id": {"type": "integer", "cardinality": 100, "distribution": "zipf"}
                    },
                }
            },
        }
    )
    scaled = scale_dataset(dataset, "orders", 5000)
    assert scaled.collections["orders"].count == 5000
    assert scaled.collections["orders"].fields["customer_id"].cardinality == 500


def test_storage_guardrail():
    dataset = DatasetConfig.model_validate(
        {
            "collections": {
                "orders": {
                    "count": 10_000_000,
                    "document_size": {"target_bytes": 2048},
                    "fields": {},
                }
            }
        }
    )
    estimate = estimate_storage(dataset)
    try:
        assert_within_limit(estimate, SafetyConfig(max_storage_gb=0.1))
        assert False, "expected guardrail"
    except GuardrailError:
        pass
    assert_within_limit(estimate, SafetyConfig(max_storage_gb=0.1), override=True)
