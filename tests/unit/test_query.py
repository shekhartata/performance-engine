import pytest

from perf_envelope.config.models import ParameterSpec, QueryConfig
from perf_envelope.query.canonicalizer import canonical_form, shape_id
from perf_envelope.query.parameters import QueryParameterGenerator
from perf_envelope.query.parser import build_filter, substitute
from perf_envelope.query.selectivity import target_match_count


def test_substitute_placeholders():
    doc = {"customer_id": {"value": "{{customer_id}}"}, "created_at": {"$gte": "{{start_date}}"}}
    out = substitute(doc, {"customer_id": 9, "start_date": "x"})
    assert out["customer_id"] == 9
    assert out["created_at"]["$gte"] == "x"


def test_canonical_ignores_literals():
    q1 = QueryConfig(
        id="q",
        collection="orders",
        filter={"customer_id": {"value": "{{customer_id}}"}, "created_at": {"$gte": "{{start_date}}"}},
        sort={"created_at": -1},
        limit=100,
    )
    q2 = QueryConfig(
        id="q",
        collection="orders",
        filter={"customer_id": {"value": "{{other}}"}, "created_at": {"$gte": "{{start_date}}"}},
        sort={"created_at": -1},
        limit=100,
    )
    assert canonical_form(q1) == canonical_form(q2)
    assert shape_id(q1) == shape_id(q2)


def test_parameter_hot_cold():
    gen = QueryParameterGenerator(
        {"customer_id": ParameterSpec(field="customer_id", strategy="hot_value")},
        cached_values={"customer_id": [1, 2, 3]},
    )
    assert gen.next()["customer_id"] == 1
    gen.specs["customer_id"] = ParameterSpec(field="customer_id", strategy="cold_value")
    assert gen.next()["customer_id"] == 3


def test_build_filter_and_selectivity_math():
    query = QueryConfig(
        id="q",
        collection="orders",
        filter={"customer_id": {"value": "{{customer_id}}"}},
    )
    assert build_filter(query, {"customer_id": 4}) == {"customer_id": 4}
    assert target_match_count(1000, 0.01) == 10


def test_query_from_mongo_json_find_and_pipeline():
    from perf_envelope.query.parser import query_from_mongo_json

    find = query_from_mongo_json('{ "_id": "{{quote_ruid}}" }', "quotes")
    assert find.query.operation == "find"
    assert find.query.filter == {"_id": "{{quote_ruid}}"}
    assert find.parameters["quote_ruid"].field == "_id"
    assert find.parameters["quote_ruid"].strategy == "selectivity_targeted"

    ranged = query_from_mongo_json(
        {
            "filter": {
                "status": "{{status}}",
                "created_at": {"$gte": "{{start_date}}"},
            },
            "limit": 50,
        },
        "quotes",
    )
    assert ranged.query.operation == "find"
    assert ranged.query.limit == 50
    assert ranged.parameters["status"].strategy == "selectivity_targeted"
    assert ranged.parameters["start_date"].type == "datetime_range"
    assert ranged.parameters["start_date"].range == ["7d", "30d", "90d"]

    pipeline = query_from_mongo_json(
        [
            {"$match": {"_id": "{{quote_ruid}}"}},
            {
                "$lookup": {
                    "from": "loans",
                    "localField": "loan_ruid",
                    "foreignField": "_id",
                    "as": "loan",
                }
            },
        ],
        "quotes",
    )
    assert pipeline.query.operation == "aggregate"
    assert pipeline.parameters["quote_ruid"].field == "_id"


def test_query_from_mongo_json_rejects_empty():
    from perf_envelope.exceptions import ConfigError
    from perf_envelope.query.parser import query_from_mongo_json

    with pytest.raises(ConfigError):
        query_from_mongo_json("  ")
    with pytest.raises(ConfigError):
        query_from_mongo_json("{not json")
