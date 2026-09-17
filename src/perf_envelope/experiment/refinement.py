"""Insert midpoint cells around GREEN→RED transitions until uncertainty ≤ 15%."""

from __future__ import annotations

from collections.abc import Iterable

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
) -> list[ExperimentCell]:
    """Propose new document-size midpoints between adjacent classifications."""
    grouped: dict[tuple, list[ExperimentCell]] = {}
    for cell in cells:
        slice_key = (cell.selectivity, cell.concurrency, cell.cache_state, tuple(sorted(cell.extras.items())))
        grouped.setdefault(slice_key, []).append(cell)

    proposed: list[ExperimentCell] = []
    seen = {cell.key() for cell in cells}
    for group in grouped.values():
        ordered = sorted(group, key=lambda c: c.documents)
        for left, right in zip(ordered, ordered[1:]):
            if right.documents <= 0 or left.documents <= 0:
                continue
            gap = (right.documents - left.documents) / right.documents
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
            midpoint = int(round((left.documents + right.documents) / 2))
            if midpoint in {left.documents, right.documents}:
                continue
            new_cell = ExperimentCell(
                documents=midpoint,
                selectivity=left.selectivity,
                concurrency=left.concurrency,
                cache_state=left.cache_state,
                extras=dict(left.extras),
            )
            if new_cell.key() not in seen:
                seen.add(new_cell.key())
                proposed.append(new_cell)
    return proposed
