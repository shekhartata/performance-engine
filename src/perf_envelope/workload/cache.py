"""Estimated cache conditioning. Atlas cannot guarantee WiredTiger residency."""

from __future__ import annotations

from typing import Any, Callable

HOT = "estimated_hot"
COLD = "estimated_cold"


def normalize_cache_state(value: str) -> str:
    lowered = str(value).lower()
    if lowered in {"hot", "estimated_hot"}:
        return HOT
    return COLD


def condition_cache(
    cache_state: str,
    warmup_fn: Callable[[], Any],
) -> str:
    """Run warmup for estimated_hot; skip it for estimated_cold."""
    state = normalize_cache_state(cache_state)
    if state == HOT:
        warmup_fn()
    return state
