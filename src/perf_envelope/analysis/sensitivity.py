"""Human-readable names for the settings a run varies."""

from __future__ import annotations

FRIENDLY = {
    "dataset_size": "Dataset size",
    "selectivity": "Selectivity",
    "concurrency": "Concurrency",
    "cache_state": "Cache condition",
    "document_size_bytes": "Document size",
    "plan": "Plan",
}


def friendly_name(name: str) -> str:
    if name in FRIENDLY:
        return FRIENDLY[name]
    raw = name[4:] if name.startswith("log_") else name
    if raw in FRIENDLY:
        return FRIENDLY[raw]
    raw = raw.replace("__", " = ")
    return raw.replace("_", " ").strip().title()
