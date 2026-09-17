from types import SimpleNamespace
from pathlib import Path

import pytest

from perf_envelope.config.models import DatasetConfig, ExternalGeneratorConfig
from perf_envelope.dataset.external import (
    collection_count,
    count_within_tolerance,
    materialize,
    render_collection_name,
    render_command,
)
from perf_envelope.environment.mongodb import redact_uri
from perf_envelope.environment.safety import CreationManifest
from perf_envelope.exceptions import ConfigError, ExecutionError


def test_render_command_substitutes_placeholders():
    rendered = render_command(
        "python gen.py --count {documents} --uri {uri} --db {database} --collection {collection}",
        documents=10000,
        uri="mongodb+srv://user:s3cret@host/db",
        database="perf_test",
        collection="orders_10000",
        seed=1,
        model="default",
    )
    assert "--count 10000" in rendered
    assert "orders_10000" in rendered
    assert "s3cret" in rendered


def test_unknown_placeholder_rejected():
    with pytest.raises(ConfigError, match="Unknown generator placeholders"):
        render_command("python gen.py --extra {hack}", documents=1, uri="u", database="d", collection="c")


def test_logged_command_redacts_uri():
    rendered = render_command(
        "python gen.py --uri {uri}",
        uri="mongodb+srv://user:s3cret@host/db",
        documents=1,
        database="d",
        collection="c",
        seed=1,
        model="m",
    )
    assert "s3cret" not in redact_uri(rendered)
    assert "***" in redact_uri(rendered)


def test_collection_template():
    assert (
        render_collection_name("{collection}_{documents}", collection="orders", documents=10000)
        == "orders_10000"
    )


def test_unknown_collection_placeholder():
    with pytest.raises(ConfigError, match="Unknown collection-template"):
        render_collection_name("{collection}_{foo}", collection="orders", documents=1)


class FakeCollection:
    def __init__(self, count: int):
        self._count = count

    def estimated_document_count(self):
        return self._count


class FakeDatabase:
    def __init__(self, counts: dict[str, int]):
        self._counts = counts

    def list_collection_names(self):
        return list(self._counts)

    def __getitem__(self, name):
        return FakeCollection(self._counts.get(name, 0))


def test_reuse_if_present_skips_subprocess(monkeypatch):
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("subprocess should not run")

    monkeypatch.setattr("perf_envelope.dataset.external.subprocess.run", boom)
    dataset = DatasetConfig(
        mode="external",
        collection="orders",
        generator=ExternalGeneratorConfig(
            command="python gen.py --count {documents} --collection {collection}",
            reuse_if_present=True,
            count_tolerance=0.05,
        ),
    )
    session = SimpleNamespace(database=FakeDatabase({"orders_10000": 10000}))
    result = materialize(
        session,
        dataset,
        documents=10000,
        collection="orders_10000",
        database="db",
        uri="mongodb://localhost",
        seed=1,
    )
    assert result["reused"] is True
    assert called["n"] == 0


def test_count_tolerance_failure(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "perf_envelope.dataset.external.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    dataset = DatasetConfig(
        mode="external",
        generator=ExternalGeneratorConfig(
            command="python gen.py --count {documents} --collection {collection}",
            reuse_if_present=False,
            count_tolerance=0.05,
        ),
    )
    session = SimpleNamespace(database=FakeDatabase({"orders_10000": 1}))
    with pytest.raises(ExecutionError, match="expected 10000"):
        materialize(
            session,
            dataset,
            documents=10000,
            collection="orders_10000",
            database="db",
            uri="mongodb://localhost",
            seed=1,
            log_dir=tmp_path,
        )


def test_nonzero_exit_is_execution_error(monkeypatch):
    monkeypatch.setattr(
        "perf_envelope.dataset.external.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=2, stdout="", stderr="boom"),
    )
    dataset = DatasetConfig(
        mode="external",
        generator=ExternalGeneratorConfig(
            command="python gen.py --count {documents}",
            reuse_if_present=False,
        ),
    )
    session = SimpleNamespace(database=FakeDatabase({}))
    with pytest.raises(ExecutionError, match="exited 2"):
        materialize(
            session,
            dataset,
            documents=10,
            collection="c",
            database="db",
            uri="mongodb://localhost",
            seed=1,
        )


def test_count_within_tolerance():
    assert count_within_tolerance(100, 100, 0.05)
    assert count_within_tolerance(96, 100, 0.05)
    assert not count_within_tolerance(80, 100, 0.05)
    assert collection_count(FakeDatabase({}), "missing") == 0


def test_manifest_records_external_collection(monkeypatch):
    monkeypatch.setattr(
        "perf_envelope.dataset.external.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    dataset = DatasetConfig(
        mode="external",
        generator=ExternalGeneratorConfig(
            command="python gen.py --count {documents} --collection {collection}",
            reuse_if_present=False,
        ),
    )
    session = SimpleNamespace(database=FakeDatabase({"c": 10}))
    manifest = CreationManifest(project="demo")
    materialize(
        session,
        dataset,
        documents=10,
        collection="c",
        database="db",
        uri="mongodb://localhost",
        seed=1,
        manifest=manifest,
    )
    assert manifest.objects[0].kind == "external_collection"
    assert manifest.objects[0].name == "c"
