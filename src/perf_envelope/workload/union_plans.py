"""Built-in server-union and application-fanout plans for the loan-instance access pattern."""

from __future__ import annotations

from typing import Any


def server_union_plan() -> dict[str, Any]:
    """One aggregation: indexed match, then `$unionWith` for the other collections, then sort."""
    return {
        "steps": [
            {
                "collection": "{{lead}}",
                "operation": "aggregate",
                "pipeline": [
                    {"$match": {"loan_instance.loan_ruid": "{{loan_ruid}}"}},
                    {
                        "$for": {"each": "collections[1:union_count]", "as": "c"},
                        "emit": {
                            "$unionWith": {
                                "coll": "{{c}}",
                                "pipeline": [
                                    {"$match": {"loan_instance.loan_ruid": "{{loan_ruid}}"}}
                                ],
                            }
                        },
                    },
                    {"$sort": {"created_at": -1}},
                ],
            }
        ],
        "combine": "concat",
    }


def app_fanout_plan() -> dict[str, Any]:
    """One find per collection, in parallel, then a client-side merge sort."""
    return {
        "steps": [
            {
                "$for": {
                    "each": "collections[0:union_count]",
                    "as": "c",
                    "parallel": True,
                },
                "emit": {
                    "collection": "{{c}}",
                    "operation": "find",
                    "filter": {"loan_instance.loan_ruid": "{{loan_ruid}}"},
                    "sort": {"created_at": -1},
                },
            }
        ],
        "combine": {"mode": "merge_sort", "field": "created_at", "direction": -1},
    }
