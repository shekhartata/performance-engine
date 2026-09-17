from pathlib import Path

from fastapi.testclient import TestClient

from perf_envelope.api.app import app
from perf_envelope.api.spec_builder import AdvancedOptions, build_spec, with_second_model
from perf_envelope.api.state import reset
from perf_envelope.config.spec import compile_spec_data
from perf_envelope.environment.mongodb import redact_uri


class _FakeAdmin:
    def command(self, *_args, **_kwargs):
        return {"ok": 1}


class _FakeCollectionList:
    def list_collection_names(self):
        return ["quotes", "loans"]


class _FakeClient:
    admin = _FakeAdmin()

    def list_database_names(self):
        return ["admin", "mi_transformation", "config"]

    def __getitem__(self, _name):
        return _FakeCollectionList()

    def close(self):
        return None


def _open(_uri, **_kwargs):
    return _FakeClient()


def test_session_redacts_password(monkeypatch):
    reset()
    monkeypatch.setattr("perf_envelope.api.app.open_client", _open)
    client = TestClient(app)
    uri = "mongodb://user:super-secret@localhost:27017"
    response = client.post("/api/session", json={"uri": uri})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "super-secret" not in response.text
    assert body["uri_redacted"] == redact_uri(uri)
    assert "mi_transformation" in body["databases"]
    assert "admin" not in body["databases"]


def test_run_requires_ack_and_session(monkeypatch):
    reset()
    monkeypatch.setattr("perf_envelope.api.app.open_client", _open)
    payload = {
        "database": "mi_transformation",
        "collection": "quotes",
        "query": {"_id": "{{quote_ruid}}"},
        "ack_non_production": False,
    }
    anon = TestClient(app)
    missing = anon.post("/api/runs", json={**payload, "ack_non_production": True})
    assert missing.status_code == 401

    client = TestClient(app)
    connected = client.post("/api/session", json={"uri": "mongodb://user:super-secret@localhost:27017"})
    assert connected.status_code == 200
    denied = client.post("/api/runs", json=payload)
    assert denied.status_code == 400
    assert "non-production" in denied.json()["detail"]
    assert "super-secret" not in denied.text


def test_build_spec_compiles_existing_with_uri():
    spec = build_spec(
        uri="mongodb://user:secret@localhost:27017",
        database="mi_transformation",
        collection="quotes",
        query={"_id": "{{quote_ruid}}"},
        advanced=AdvancedOptions(),
    )
    resolved = compile_spec_data(spec, source=Path("ui-run.yaml"))
    assert resolved.dataset.mode == "existing"
    assert resolved.environment.connection.uri == "mongodb://user:secret@localhost:27017"
    assert resolved.query.collection == "quotes"
    assert resolved.parameters["quote_ruid"].field == "_id"
    assert resolved.experiment.dimensions.cache_state.values == ["hot", "cold"]
    assert resolved.experiment.dimensions.selectivity.values == [0.01, 0.1]
    alt = with_second_model(resolved, [{"$match": {"_id": "{{id}}"}}], "quotes", "lookup")
    assert "lookup" in alt.model_names
    assert alt.queries["lookup"].operation == "aggregate"


def test_build_spec_synthetic_sweeps_selectivity_and_document_size():
    spec = build_spec(
        uri="mongodb://user:secret@localhost:27017",
        database="perf_test",
        collection="main",
        query={
            "filter": {
                "group_id": "{{group_id}}",
                "created_at": {"$gte": "{{start_date}}"},
            },
            "limit": 50,
        },
        advanced=AdvancedOptions(
            mode="synthetic",
            documents="1000, 5000",
            selectivity="0.01, 0.05, 0.1",
            concurrency="1, 4",
        ),
    )
    resolved = compile_spec_data(spec, source=Path("ui-run.yaml"))
    assert resolved.dataset.mode == "synthetic"
    assert "group_id" in resolved.dataset.collections["main"].fields
    assert resolved.experiment.dimensions.selectivity.values == [0.01, 0.05, 0.1]
    assert resolved.experiment.dimensions.document_size.values == [512, 2048]
    assert resolved.parameters["group_id"].strategy == "selectivity_targeted"
    assert resolved.parameters["start_date"].type == "datetime_range"
    assert resolved.query.limit == 50
