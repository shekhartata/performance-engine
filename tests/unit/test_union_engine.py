"""Shape templating, request plans, pool sizing, explain stages, and thresholds."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from perf_envelope.analysis.features import feature_matrix
from perf_envelope.analysis.pipeline import analyze_frame
from perf_envelope.config.models import CloneEmbedRecipe, EmbedSpec, ExperimentDimensions
from perf_envelope.config.spec import compile_spec
from perf_envelope.dataset.recipe import choose_embeds, collection_names
from perf_envelope.environment.pool import pool_formula, required_pool_size
from perf_envelope.experiment.matrix import build_matrix
from perf_envelope.experiment.planner import ExperimentPlanner
from perf_envelope.experiment.refinement import propose_refinement
from perf_envelope.experiment.settings_log import cell_settings, run_level_settings
from perf_envelope.frontier.threshold import find_thresholds
from perf_envelope.query.parser import render_for_cell
from perf_envelope.telemetry.explain import walk_explain
from perf_envelope.workload.plan import execute_plan, render_plan
from perf_envelope.workload.union_plans import app_fanout_plan, server_union_plan

ROOT = Path(__file__).resolve().parents[2]


def test_render_for_cell_types_slices_and_for():
    rendered = render_for_cell(
        [
            {"$match": {"id": "{{loan_ruid}}"}},
            {"$limit": "{{limit}}"},
            {
                "$for": {"each": "collections[1:union_count]", "as": "c"},
                "emit": {"$unionWith": {"coll": "{{c}}"}},
            },
        ],
        {"collections": ["a", "b", "c"], "union_count": 3, "limit": 10},
    )
    assert rendered[0]["$match"]["id"] == "{{loan_ruid}}"
    assert rendered[1] == {"$limit": 10}
    assert [stage["$unionWith"]["coll"] for stage in rendered[2:]] == ["b", "c"]


def test_server_and_app_plans_follow_n():
    variables = {"collections": ["e1", "e2", "e3"], "union_count": 2, "lead": "e1"}
    server = render_plan(server_union_plan(), variables)
    assert server.steps[0].collection == "e1"
    unions = [stage for stage in server.steps[0].pipeline if "$unionWith" in stage]
    assert [stage["$unionWith"]["coll"] for stage in unions] == ["e2"]

    app = render_plan(app_fanout_plan(), variables)
    assert [step.collection for step in app.steps] == ["e1", "e2"]
    assert all(step.parallel for step in app.steps)
    assert app.combine.mode == "merge_sort"


class _Collection:
    def __init__(self, docs: list[dict]):
        self.docs = docs

    def find(self, filt, **kwargs):
        wanted = filt.get("loan_instance.loan_ruid")
        return [doc for doc in self.docs if doc["loan_instance"]["loan_ruid"] == wanted]


def test_app_fanout_merges_and_sorts():
    database = {
        "e1": _Collection([{"created_at": 1, "loan_instance": {"loan_ruid": "k"}}]),
        "e2": _Collection([{"created_at": 3, "loan_instance": {"loan_ruid": "k"}}]),
    }
    plan = render_plan(
        app_fanout_plan(),
        {"collections": ["e1", "e2"], "union_count": 2, "lead": "e1"},
    )
    result = execute_plan(database, plan, {"loan_ruid": "k"}, timeout_ms=1000)
    assert result.ok
    assert result.returned == 2
    assert len(result.step_ms) == 2
    ordered = sorted(
        [
            {"created_at": 1},
            {"created_at": 3},
        ],
        key=lambda doc: doc["created_at"],
        reverse=True,
    )
    assert [doc["created_at"] for doc in ordered] == [3, 1]


def test_pool_size_formula():
    assert required_pool_size(concurrency=8, parallel_steps=3, headroom=4) == 28
    assert "concurrency 8" in pool_formula(8, 3, 4)
    assert "parallel steps 3" in pool_formula(8, 3, 4)
    assert required_pool_size(1, 1, 4) == 5


def test_explain_walks_union_branches_and_sort():
    raw = {
        "stages": [
            {
                "$cursor": {
                    "executionStats": {
                        "nReturned": 2,
                        "totalKeysExamined": 2,
                        "totalDocsExamined": 2,
                        "executionTimeMillis": 4,
                    }
                }
            },
            {
                "$unionWith": {
                    "coll": "entity_02",
                    "pipeline": [
                        {
                            "$cursor": {
                                "executionStats": {
                                    "nReturned": 2,
                                    "totalKeysExamined": 5,
                                    "totalDocsExamined": 2,
                                    "executionTimeMillis": 6,
                                    "executionStages": {
                                        "stage": "SORT",
                                        "nReturned": 4,
                                        "usedDisk": True,
                                        "memLimit": 100,
                                        "totalDataSizeSorted": 400,
                                    },
                                }
                            }
                        }
                    ],
                }
            },
        ]
    }
    walked = walk_explain(raw)
    assert walked["n_returned"] == 4
    assert walked["keys_examined"] == 7
    assert len(walked["branches"]) == 2
    assert walked["sort"]["used_disk"] is True


def test_threshold_is_last_n_under_slo_per_plan():
    frame = pd.DataFrame(
        {
            "model_id": ["server", "server", "server", "app", "app", "app"],
            "dataset_size": [100] * 6,
            "union_count": [1, 4, 8, 1, 4, 8],
            "p95_ms": [20, 40, 150, 10, 30, 50],
        }
    )
    rows = find_thresholds(frame, "union_count", slo_p95=100, for_each=["documents", "plan"])
    by_plan = {row["plan"]: row["max_meeting_slo"] for row in rows}
    assert by_plan["server"] == 4
    assert by_plan["app"] == 8
    assert all(row["dataset_size"] == 100 for row in rows)


def test_thresholds_are_per_condition_not_averaged():
    frame = pd.DataFrame(
        {
            "model_id": ["app"] * 6,
            "dataset_size": [100] * 6,
            "concurrency": [1, 1, 1, 8, 8, 8],
            "union_count": [1, 4, 8, 1, 4, 8],
            "matches_per_key": [1] * 6,
            "p95_ms": [20, 30, 40, 60, 150, 300],
        }
    )
    analysis = analyze_frame(
        frame,
        slo_p95=100,
        sweep_axes=["union_count", "matches_per_key"],
        goal={"axis": "union_count", "for_each": ["documents"]},
    )
    by_concurrency = {row["concurrency"]: row["max_meeting_slo"] for row in analysis["thresholds"]}
    assert by_concurrency == {1: 8, 8: 1}


def test_refinement_bisects_union_count():
    dims = ExperimentDimensions.model_validate(
        {
            "documents": {"values": [100]},
            "selectivity": {"values": [0.5]},
            "concurrency": {"values": [1]},
            "cache_state": {"values": ["hot"]},
            "extra_axes": {"union_count": {"values": [1, 8], "bind": "shape"}},
        }
    )
    cells = build_matrix(dims)
    assert cells[0].bindings["union_count"] == "shape"
    low, high = sorted(cells, key=lambda cell: cell.extras["union_count"])
    proposed = propose_refinement(
        [low, high],
        {low.key(): 10.0, high.key(): 200.0},
        slo_p95=100,
        axis="union_count",
    )
    assert proposed
    assert proposed[0].extras["union_count"] == 4
    assert proposed[0].documents == 100


def test_sweep_axis_becomes_a_model_feature():
    frame = pd.DataFrame(
        {
            "dataset_size": [100, 100, 100, 100],
            "selectivity": [0.5, 0.5, 0.5, 0.5],
            "concurrency": [1, 1, 1, 1],
            "cache_state": ["estimated_hot"] * 4,
            "document_size_bytes": [512] * 4,
            "result_count": [1, 1, 1, 1],
            "union_count": [1, 2, 4, 8],
            "p95_ms": [5, 8, 12, 20],
        }
    )
    _, _, names = feature_matrix(frame, ["union_count"])
    assert "log_union_count" in names


def test_run_settings_mark_user_default_and_derived_pool():
    resolved = compile_spec(ROOT / "examples" / "union_fanout.yaml")
    rows = {row["setting"]: row for row in run_level_settings(resolved, rtt_ms=12.5, host={"hostname": "test"})}
    assert rows["workload.duration_seconds"]["source"] == "user"
    assert rows["workload.duration_seconds"]["value"] == 8
    assert rows["workload.timeout_ms"]["source"] == "user"
    assert rows["workload.think_time_ms"]["source"] == "default"
    assert rows["connection_pool.max_size"]["source"] == "derived"
    assert "per test case" in rows["connection_pool.max_size"]["value"]
    cell = cell_settings(
        model="app_fanout",
        cell_values={"documents": 100000, "union_count": 8, "concurrency": 1},
        bindings={"union_count": "shape", "concurrency": "load"},
        database="mi_quotes_100000",
        collections=["perfenv_union_k1_entity_01"],
        rendered={"steps": []},
        concurrency=1,
        parallel_steps=8,
        headroom=4,
        pool_mode="derived",
        fixed_pool_size=100,
        pool_stats={"peak_in_use": 8, "wait_p95_ms": 0.4, "wait_p50_ms": 0.1, "wait_max_ms": 0.5, "connections_created": 0, "checkouts": 8},
        warmup=True,
        sampled_parameters={"loan_ruid": {"distinct": 3, "samples": 10, "top": []}},
        client_cpu_pct=4.2,
        rtt_ms=12.5,
    )
    pool = next(row for row in cell["settings"] if row["setting"] == "connection_pool.max_size")
    assert pool["source"] == "derived"
    assert pool["value"] == 12
    assert "concurrency 1" in pool["formula"]
    assert "parallel steps 8" in pool["formula"]


def test_union_spec_compiles_two_plans():
    resolved = compile_spec(ROOT / "examples" / "union_fanout.yaml")
    assert set(resolved.model_names) == {"server_union", "app_fanout"}
    assert resolved.experiment.goal is not None
    assert resolved.experiment.goal.axis == "union_count"
    cells = ExperimentPlanner(resolved).coarse_cells()
    assert {cell.extras["union_count"] for cell in cells} == {1, 2, 4, 8}
    assert resolved.dataset.recipe is not None
    assert collection_names(resolved.dataset.recipe, 1)[0] == "perfenv_union_k1_entity_01"
    recipe = CloneEmbedRecipe(
        source_collection="quotes",
        embed=EmbedSpec(source="mi_transformation.loan_instances"),
        copies=2,
    )
    assert collection_names(recipe, 50) == [
        "perfenv_union_k50_entity_01",
        "perfenv_union_k50_entity_02",
    ]


def test_lookup_keys_are_unique_when_the_embed_source_is_smaller():
    embeds = [{"loan_ruid": "a", "_id": 1}, {"loan_ruid": "b", "_id": 2}]
    exact = choose_embeds(embeds, 2)
    assert [doc["loan_ruid"] for doc in exact] == ["a", "b"]
    stretched = choose_embeds(embeds, 4)
    assert len({doc["loan_ruid"] for doc in stretched}) == 4
    assert all(doc["_id"] in {1, 2} for doc in stretched)
