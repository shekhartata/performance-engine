"""Model-level influence metrics. Not causal proof."""

from __future__ import annotations

from perf_envelope.analysis.xgboost_model import BoostedFit

FRIENDLY = {
    "log_dataset_size": "Dataset size",
    "log_selectivity": "Selectivity",
    "log_concurrency": "Concurrency",
    "cache_state_hot": "Cache condition",
    "log_document_size": "Document size",
    "log_result_cardinality": "Result cardinality",
}


def friendly_name(name: str) -> str:
    if name in FRIENDLY:
        return FRIENDLY[name]
    raw = name[4:] if name.startswith("log_") else name
    raw = raw.replace("__", " = ")
    return raw.replace("_", " ").strip().title()


def relative_sensitivity(fit: BoostedFit | None) -> dict[str, float]:
    if fit is None or not fit.importances:
        return {}
    total = sum(max(v, 0.0) for v in fit.importances.values()) or 1.0
    return {
        friendly_name(name): round(100.0 * max(value, 0.0) / total, 1)
        for name, value in sorted(fit.importances.items(), key=lambda kv: -kv[1])
    }
