"""MongoDB client helpers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from pymongo import MongoClient
from pymongo.database import Database

from perf_envelope.config.models import EnvironmentConfig
from perf_envelope.exceptions import EnvironmentError


def redact_uri(uri: str) -> str:
    return re.sub(r"(://[^:/@]+:)([^@]+)(@)", r"\1***\3", uri)


def normalize_uri(uri: str) -> str:
    """Accept a raw Atlas URI, a quoted .env value, or `MONGODB_URI=...`."""
    text = (uri or "").strip()
    if text.startswith("MONGODB_URI"):
        _, _, rest = text.partition("=")
        text = rest.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def resolve_uri(config: EnvironmentConfig, environ: dict[str, str] | None = None) -> str:
    if config.connection.uri:
        return config.connection.uri
    env = environ if environ is not None else os.environ
    key = config.connection.uri_env
    uri = env.get(key)
    if not uri:
        raise EnvironmentError(
            f"MongoDB URI not found in environment variable {key}. "
            "Set it to your Atlas connection string."
        )
    return uri


SYSTEM_DATABASES = frozenset({"admin", "local", "config"})


def open_client(
    uri: str,
    *,
    max_pool_size: int = 100,
    server_selection_timeout_ms: int = 15_000,
) -> MongoClient:
    client = MongoClient(
        uri,
        maxPoolSize=max_pool_size,
        serverSelectionTimeoutMS=server_selection_timeout_ms,
        appname="perf-envelope",
    )
    try:
        client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        client.close()
        raise EnvironmentError(f"Failed to connect to MongoDB: {exc}") from exc
    return client


def list_user_databases(client: MongoClient) -> list[str]:
    return sorted(name for name in client.list_database_names() if name not in SYSTEM_DATABASES)


def list_collections(client: MongoClient, database: str) -> list[str]:
    return sorted(client[database].list_collection_names())


@dataclass
class MongoSession:
    client: MongoClient
    database: Database
    uri_redacted: str

    def close(self) -> None:
        self.client.close()


def connect(
    config: EnvironmentConfig,
    *,
    max_pool_size: int = 100,
    environ: dict[str, str] | None = None,
) -> MongoSession:
    uri = resolve_uri(config, environ)
    try:
        client = open_client(uri, max_pool_size=max_pool_size)
    except EnvironmentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EnvironmentError(f"Failed to connect to MongoDB: {exc}") from exc
    database = client[config.connection.database]
    return MongoSession(client=client, database=database, uri_redacted=redact_uri(uri))
