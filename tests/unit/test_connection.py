import pytest

from perf_envelope.config.models import ConnectionConfig, EnvironmentConfig
from perf_envelope.environment.mongodb import redact_uri, resolve_uri
from perf_envelope.exceptions import EnvironmentError


def test_resolve_uri_prefers_explicit_uri():
    config = EnvironmentConfig(
        connection=ConnectionConfig(uri="mongodb://user:secret@localhost:27017", uri_env="MONGODB_URI")
    )
    assert resolve_uri(config, environ={}) == "mongodb://user:secret@localhost:27017"
    assert resolve_uri(config, environ={"MONGODB_URI": "mongodb://env"}) == "mongodb://user:secret@localhost:27017"


def test_resolve_uri_falls_back_to_env():
    config = EnvironmentConfig(connection=ConnectionConfig(uri_env="MONGODB_URI"))
    assert resolve_uri(config, environ={"MONGODB_URI": "mongodb://from-env"}) == "mongodb://from-env"
    with pytest.raises(EnvironmentError):
        resolve_uri(config, environ={})


def test_redact_uri_hides_password():
    assert "secret" not in redact_uri("mongodb://user:secret@localhost:27017")
    assert redact_uri("mongodb://user:secret@localhost:27017").startswith("mongodb://user:***")


def test_normalize_uri_strips_env_quotes():
    from perf_envelope.environment.mongodb import normalize_uri

    raw = "mongodb+srv://user:pass@cluster.mongodb.net/"
    assert normalize_uri(f'"{raw}"') == raw
    assert normalize_uri(f"MONGODB_URI={raw}") == raw
    assert normalize_uri(f"MONGODB_URI=\"{raw}\"") == raw
