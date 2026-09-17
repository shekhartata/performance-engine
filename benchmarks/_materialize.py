#!/usr/bin/env python3
"""Materialize the V1 built-in benchmark suite as portable projects.

Each benchmark uses the generic single-collection layout: the model, dataset,
indexes and query files are named after their role (model.yaml, dataset.yaml,
indexes.yaml, query.yaml) and target the default logical collection ("main"),
so nothing is tied to a particular domain.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1] / "benchmarks"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip())


def common(project: str, db: str = "perf_test") -> None:
    base = ROOT / project
    write(
        base / "project.yaml",
        f"""
        name: {project}
        environment: atlas-test
        default_slo: default
        safety:
          non_production: true
          managed_prefix: perfenv
          max_storage_gb: 2
        execution:
          warmup_queries: 5
          explain_samples: 2
          repetitions: 2
          max_refinement_rounds: 1
          insert_batch_size: 200
          runs_dir: runs
        """,
    )
    write(
        base / "environments/atlas-test.yaml",
        f"""
        environment:
          engine: mongodb
          connection:
            uri_env: MONGODB_URI
            database: {db}
          safety:
            non_production: true
        """,
    )
    write(
        base / "slo/default.yaml",
        """
        slo:
          latency:
            p95_ms: 100
            p99_ms: 250
          error_rate:
            maximum: 0.01
        """,
    )
    write(
        base / "workloads/read.yaml",
        """
        workload:
          mode: closed_loop
          duration_seconds: 5
          concurrency: 2
          timeout_ms: 20000
          warmup_queries: 5
          explain_samples: 2
        """,
    )


def experiment(project: str) -> None:
    write(
        ROOT / project / "experiments/scale.yaml",
        f"""
        experiment:
          id: {project}
          model: model
          dataset: dataset
          query: query
          indexes: indexes
          workload: read
          slo: default
          dimensions:
            documents:
              values: [200, 800]
            selectivity:
              values: [0.01, 0.1]
            concurrency:
              values: [1, 4]
            cache_state:
              values: [hot, cold]
          repetitions: 2
        """,
    )


def main() -> None:
    # A — indexed equality
    common("a_indexed_equality")
    write(
        ROOT / "a_indexed_equality/models/model.yaml",
        """
        model:
          fields:
            key: integer
            label: string
        """,
    )
    write(
        ROOT / "a_indexed_equality/indexes/indexes.yaml",
        """
        indexes:
          - name: key_idx
            keys:
              key: 1
        """,
    )
    write(
        ROOT / "a_indexed_equality/datasets/dataset.yaml",
        """
        dataset:
          mode: synthetic
          seed: 1
          count: 400
          fields:
            key:
              type: integer
              cardinality: 400
              distribution: sequential
            label:
              type: string
              cardinality: 400
        """,
    )
    write(
        ROOT / "a_indexed_equality/queries/query.yaml",
        """
        query:
          id: point_lookup
          operation: find
          filter:
            key:
              value: "{{key}}"
          limit: 1
        parameters:
          key:
            type: dataset_value
            field: key
            strategy: random_existing
        """,
    )
    experiment("a_indexed_equality")

    # B — equality + range
    common("b_equality_range")
    write(
        ROOT / "b_equality_range/models/model.yaml",
        """
        model:
          fields:
            group_id: integer
            created_at: datetime
            amount: float
        """,
    )
    write(
        ROOT / "b_equality_range/indexes/indexes.yaml",
        """
        indexes:
          - name: group_recent
            keys:
              group_id: 1
              created_at: -1
        """,
    )
    write(
        ROOT / "b_equality_range/datasets/dataset.yaml",
        """
        dataset:
          mode: synthetic
          seed: 2
          count: 600
          fields:
            group_id:
              type: integer
              cardinality: 80
              distribution: zipf
            created_at:
              type: datetime
              start: 365d
            amount:
              type: float
              min: 1
              max: 500
        """,
    )
    write(
        ROOT / "b_equality_range/queries/query.yaml",
        """
        query:
          id: equality_range
          operation: find
          filter:
            group_id:
              value: "{{group_id}}"
            created_at:
              $gte: "{{start_date}}"
          sort:
            created_at: -1
          limit: 50
        parameters:
          group_id:
            type: dataset_value
            field: group_id
            strategy: selectivity_targeted
          start_date:
            type: datetime_range
            strategy: range
            range: [7d, 30d, 90d]
        """,
    )
    experiment("b_equality_range")

    # C — filter + sort
    common("c_filter_sort")
    write(
        ROOT / "c_filter_sort/models/model.yaml",
        """
        model:
          fields:
            tenant_id: integer
            timestamp: datetime
        """,
    )
    write(
        ROOT / "c_filter_sort/indexes/indexes.yaml",
        """
        indexes:
          - name: tenant_ts
            keys:
              tenant_id: 1
              timestamp: -1
        """,
    )
    write(
        ROOT / "c_filter_sort/datasets/dataset.yaml",
        """
        dataset:
          mode: synthetic
          seed: 3
          count: 600
          fields:
            tenant_id:
              type: integer
              cardinality: 20
              distribution: zipf
              alpha: 1.4
            timestamp:
              type: datetime
              start: 90d
        """,
    )
    write(
        ROOT / "c_filter_sort/queries/query.yaml",
        """
        query:
          id: filter_sort
          operation: find
          filter:
            tenant_id:
              value: "{{tenant_id}}"
          sort:
            timestamp: -1
          limit: 100
        parameters:
          tenant_id:
            type: dataset_value
            field: tenant_id
            strategy: hot_value
        """,
    )
    experiment("c_filter_sort")

    # D — multikey
    common("d_multikey")
    write(
        ROOT / "d_multikey/models/model.yaml",
        """
        model:
          fields:
            key: string
            tags: array
        """,
    )
    write(
        ROOT / "d_multikey/indexes/indexes.yaml",
        """
        indexes:
          - name: tags_idx
            keys:
              tags: 1
        """,
    )
    write(
        ROOT / "d_multikey/datasets/dataset.yaml",
        """
        dataset:
          mode: synthetic
          seed: 4
          count: 400
          fields:
            key:
              type: string
              cardinality: 400
            tags:
              type: array
              array_size: [2, 8]
              items:
                type: string
                cardinality: 30
                distribution: zipf
        """,
    )
    write(
        ROOT / "d_multikey/queries/query.yaml",
        """
        query:
          id: multikey_contains
          operation: find
          filter:
            tags:
              value: "{{tag}}"
          limit: 50
        parameters:
          tag:
            type: dataset_value
            field: tags
            strategy: random_existing
        """,
    )
    experiment("d_multikey")

    # E — pointer to getting-started plus a local stub that validates
    common("e_embedded_vs_referenced")
    write(
        ROOT / "e_embedded_vs_referenced/README.md",
        """
        # Benchmark E

        Full embedded vs referenced comparison lives in `examples/getting-started`.
        This stub keeps the suite discoverable.
        """,
    )
    write(
        ROOT / "e_embedded_vs_referenced/models/model.yaml",
        """
        model:
          fields:
            group_id: integer
            created_at: datetime
        """,
    )
    write(
        ROOT / "e_embedded_vs_referenced/indexes/indexes.yaml",
        """
        indexes:
          - name: group_recent
            keys:
              group_id: 1
              created_at: -1
        """,
    )
    write(
        ROOT / "e_embedded_vs_referenced/datasets/dataset.yaml",
        """
        dataset:
          mode: synthetic
          seed: 5
          count: 400
          fields:
            group_id:
              type: integer
              cardinality: 50
              distribution: zipf
            created_at:
              type: datetime
              start: 90d
        """,
    )
    write(
        ROOT / "e_embedded_vs_referenced/queries/query.yaml",
        """
        query:
          id: recent_by_group
          operation: find
          filter:
            group_id:
              value: "{{group_id}}"
          sort:
            created_at: -1
          limit: 100
        parameters:
          group_id:
            type: dataset_value
            field: group_id
            strategy: random_existing
        """,
    )
    experiment("e_embedded_vs_referenced")

    # F — poor access pattern
    common("f_poor_access")
    write(
        ROOT / "f_poor_access/models/model.yaml",
        """
        model:
          fields:
            status: string
            note: string
        """,
    )
    write(
        ROOT / "f_poor_access/indexes/indexes.yaml",
        """
        indexes: []
        """,
    )
    write(
        ROOT / "f_poor_access/datasets/dataset.yaml",
        """
        dataset:
          mode: synthetic
          seed: 6
          count: 500
          fields:
            status:
              type: categorical
              values:
                OPEN: 0.95
                CLOSED: 0.05
            note:
              type: string
              cardinality: 500
        """,
    )
    write(
        ROOT / "f_poor_access/queries/query.yaml",
        """
        query:
          id: unselective_scan
          operation: find
          filter:
            status:
              value: "{{status}}"
        parameters:
          status:
            type: literal
            strategy: fixed
            value: OPEN
        """,
    )
    experiment("f_poor_access")


if __name__ == "__main__":
    main()
