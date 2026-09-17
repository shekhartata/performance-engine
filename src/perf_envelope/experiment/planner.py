"""Plan the coarse experiment matrix without executing it."""

from __future__ import annotations

from typing import Any

from perf_envelope.config.loader import ResolvedExperiment
from perf_envelope.config.models import DEFAULT_COLLECTION, DimensionSpec, primary_collection
from perf_envelope.dataset.external import render_collection_name
from perf_envelope.exceptions import ConfigError
from perf_envelope.experiment.matrix import ExperimentCell, build_matrix


class ExperimentPlanner:
    def __init__(self, resolved: ResolvedExperiment):
        self.resolved = resolved

    @property
    def targets_existing(self) -> bool:
        return self.resolved.dataset.mode == "existing"

    @property
    def targets_external(self) -> bool:
        return self.resolved.dataset.mode == "external"

    @property
    def targets_existing_scales(self) -> bool:
        return self.targets_existing and self.resolved.dataset.has_scale_map

    def coarse_cells(self) -> list[ExperimentCell]:
        defaults = {
            "documents": self._default_documents(),
            "concurrency": self.resolved.workload.concurrency,
            "selectivity": 0.01,
            "cache_state": "hot",
        }
        dimensions = self.resolved.experiment.dimensions
        if self.targets_existing_scales:
            # Multi-DB existing: keep (or derive) a documents axis from the scale map.
            scale_sizes = sorted(self.resolved.dataset.scales)
            if dimensions.documents is None:
                dimensions = dimensions.model_copy(
                    update={"documents": DimensionSpec(values=scale_sizes)}
                )
            else:
                requested = [int(value) for value in dimensions.documents.values]
                missing = [size for size in requested if size not in self.resolved.dataset.scales]
                if missing:
                    raise ConfigError(
                        "documents sweep includes sizes without dataset.scales entries: "
                        + ", ".join(str(size) for size in missing)
                    )
                dimensions = dimensions.model_copy(
                    update={"documents": DimensionSpec(values=requested)}
                )
        elif self.targets_existing:
            # Single existing collection: size is fixed, so drop the documents sweep.
            dimensions = dimensions.model_copy(update={"documents": None})
        return build_matrix(dimensions, defaults)

    def _default_documents(self) -> int:
        dataset = self.resolved.dataset
        if dataset.collections:
            primary = self.resolved.query.collection or primary_collection(dataset)
            if primary in dataset.collections:
                return dataset.collections[primary].count
            return next(iter(dataset.collections.values())).count
        # Existing collections report their real size at run time.
        return 0

    def _logical_collection(self) -> str:
        dataset = self.resolved.dataset
        return (
            dataset.collection
            or self.resolved.query.collection
            or primary_collection(dataset)
            or DEFAULT_COLLECTION
        )

    def per_scale_collections(self) -> list[dict[str, Any]]:
        if not self.targets_external:
            return []
        dataset = self.resolved.dataset
        logical = self._logical_collection()
        dimensions = self.resolved.experiment.dimensions
        sizes = list(dimensions.documents.values) if dimensions.documents else []
        rows = []
        for size in sizes:
            name = render_collection_name(
                dataset.collection_template,
                collection=logical,
                documents=int(size),
            )
            rows.append({"documents": int(size), "collection": name})
        return rows

    def plan_dict(self) -> dict[str, Any]:
        cells = self.coarse_cells()
        payload = {
            "experiment_id": self.resolved.experiment.id,
            "models": self.resolved.model_names,
            "dataset_mode": self.resolved.dataset.mode,
            "target_database": self.resolved.environment.connection.database,
            "cell_count": len(cells) * max(len(self.resolved.model_names), 1) * self.resolved.execution.repetitions,
            "coarse_cells": len(cells),
            "repetitions": self.resolved.execution.repetitions,
            "cells": [cell.as_dict() for cell in cells],
        }
        if self.resolved.experiment.project_documents:
            payload["project_documents"] = list(self.resolved.experiment.project_documents)
        if self.targets_existing_scales:
            payload["target_collection"] = self._logical_collection()
            payload["dataset_size_sweep"] = "enabled (existing multi-database scales)"
            payload["scale_databases"] = {
                str(size): name for size, name in sorted(self.resolved.dataset.scales.items())
            }
        elif self.targets_existing:
            payload["target_collection"] = self._logical_collection()
            payload["dataset_size_sweep"] = "disabled (fixed by existing collection)"
            for cell in payload["cells"]:
                cell["documents"] = "measured at run time"
        if self.targets_external:
            payload["dataset_size_sweep"] = "enabled (external generator)"
            payload["per_scale_collections"] = self.per_scale_collections()
        return payload
