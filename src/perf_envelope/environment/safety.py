"""Safety gates, managed-object naming, and creation manifests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from perf_envelope.config.models import SafetyConfig
from perf_envelope.exceptions import SafetyError

MANAGED_COLLECTION_PATTERN = re.compile(r"^perfenv_[a-zA-Z0-9_]+$")


def sanitize_token(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return cleaned or "project"


def managed_collection_name(project_name: str, logical_name: str, prefix: str = "perfenv") -> str:
    return f"{sanitize_token(prefix)}_{sanitize_token(project_name)}_{sanitize_token(logical_name)}"


def is_managed_collection(name: str) -> bool:
    return bool(MANAGED_COLLECTION_PATTERN.match(name)) or name.startswith("perfenv_")


def assert_non_production(safety: SafetyConfig, acknowledged: bool) -> None:
    if not safety.non_production:
        raise SafetyError(
            "Refusing to run: environment.safety.non_production is not true. "
            "This engine targets non-production Atlas clusters only."
        )
    if not acknowledged:
        raise SafetyError(
            "Refusing destructive or load-generating action. Re-run with "
            "--ack-non-production to confirm this is a non-production Atlas cluster."
        )


def assert_managed(collection_name: str, *, unmanaged_allowed: bool = False) -> None:
    if unmanaged_allowed:
        return
    if not is_managed_collection(collection_name):
        raise SafetyError(
            f"Refusing to mutate unmanaged collection '{collection_name}'. "
            "Synthetic datasets must use the perfenv_<project>_<collection> prefix."
        )


@dataclass
class CreatedObject:
    kind: str
    name: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CreationManifest:
    project: str
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    objects: list[CreatedObject] = field(default_factory=list)

    def add(self, kind: str, name: str, **details: Any) -> None:
        self.objects.append(CreatedObject(kind=kind, name=name, details=details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "created_at": self.created_at,
            "objects": [
                {"kind": obj.kind, "name": obj.name, **obj.details} for obj in self.objects
            ],
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))

    @classmethod
    def load(cls, path: Path) -> "CreationManifest":
        raw = json.loads(path.read_text())
        manifest = cls(project=raw.get("project", "unknown"), created_at=raw.get("created_at", ""))
        for obj in raw.get("objects", []):
            kind = obj.pop("kind")
            name = obj.pop("name")
            manifest.add(kind, name, **obj)
        return manifest
