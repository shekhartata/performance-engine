"""Insert midpoint cells around GREEN→RED transitions until uncertainty ≤ 15%."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from perf_envelope.experiment.matrix import ExperimentCell


def classify(p95_ms: float, slo_p95: float, green_fraction: float = 0.7) -> str:
    if p95_ms <= slo_p95 * green_fraction:
        return "GREEN"
    if p95_ms <= slo_p95:
        return "AMBER"
    return "RED"


def propose_refinement(
    cells: Iterable[ExperimentCell],
    p95_by_key: dict[tuple, float],
    slo_p95: float,
    *,
    uncertainty: float = 0.15,
    green_fraction: float = 0.7,
    axis: str = "documents",
) -> list[ExperimentCell]:
    """Propose midpoints along `axis` between adjacent classifications."""
    grouped: dict[tuple, list[ExperimentCell]] = {}
    for cell in cells:
        grouped.setdefault(_slice_key(cell, axis), []).append(cell)

    proposed: list[ExperimentCell] = []
    seen = {cell.key() for cell in cells}
    for group in grouped.values():
        ordered = sorted(group, key=lambda cell: _axis_value(cell, axis))
        for left, right in zip(ordered, ordered[1:]):
            left_value = _axis_value(left, axis)
            right_value = _axis_value(right, axis)
            if right_value <= 0 or left_value < 0 or right_value <= left_value:
                continue
            gap = (right_value - left_value) / right_value
            if gap <= uncertainty:
                continue
            left_p95 = p95_by_key.get(left.key())
            right_p95 = p95_by_key.get(right.key())
            if left_p95 is None or right_p95 is None:
                continue
            left_cls = classify(left_p95, slo_p95, green_fraction)
            right_cls = classify(right_p95, slo_p95, green_fraction)
            if left_cls == right_cls == "GREEN":
                continue
            if left_cls == right_cls == "RED":
                continue
            midpoint = (left_value + right_value) / 2
            if isinstance(left_value, int) and isinstance(right_value, int):
                midpoint = int(round(midpoint))
            if midpoint in {left_value, right_value}:
                continue
            new_cell = _with_axis(left, axis, midpoint)
            if new_cell.key() not in seen:
                seen.add(new_cell.key())
                proposed.append(new_cell)
    return proposed


def _slice_key(cell: ExperimentCell, axis: str) -> tuple:
    extras = tuple(sorted((key, str(value)) for key, value in cell.extras.items() if key != axis))
    parts: list[Any] = []
    if axis != "documents":
        parts.append(cell.documents)
    if axis != "selectivity":
        parts.append(cell.selectivity)
    if axis != "concurrency":
        parts.append(cell.concurrency)
    if axis != "cache_state":
        parts.append(cell.cache_state)
    parts.append(extras)
    return tuple(parts)


def _axis_value(cell: ExperimentCell, axis: str) -> float:
    if axis == "documents":
        return float(cell.documents)
    if axis == "selectivity":
        return float(cell.selectivity)
    if axis == "concurrency":
        return float(cell.concurrency)
    value = cell.extras.get(axis, 0)
    return float(value)


def _with_axis(cell: ExperimentCell, axis: str, value: float) -> ExperimentCell:
    documents = cell.documents
    selectivity = cell.selectivity
    concurrency = cell.concurrency
    extras = dict(cell.extras)
    if axis == "documents":
        documents = int(round(value))
    elif axis == "selectivity":
        selectivity = float(value)
    elif axis == "concurrency":
        concurrency = int(round(value))
    else:
        current = cell.extras.get(axis)
        extras[axis] = int(round(value)) if isinstance(current, int) else float(value)
    return ExperimentCell(
        documents=documents,
        selectivity=selectivity,
        concurrency=concurrency,
        cache_state=cell.cache_state,
        extras=extras,
        bindings=dict(cell.bindings),
    )
