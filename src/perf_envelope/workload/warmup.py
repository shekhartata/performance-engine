"""Warm-up queries that never enter benchmark statistics."""

from __future__ import annotations

from collections.abc import Callable


def warmup(times: int, fn: Callable[[], object]) -> None:
    for _ in range(max(0, times)):
        try:
            fn()
        except Exception:  # noqa: BLE001 — warm-up failures are ignored
            continue
