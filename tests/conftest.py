import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "getting-started"


@pytest.fixture
def example_project() -> Path:
    return EXAMPLE


@pytest.fixture
def mongo_uri() -> str | None:
    return os.environ.get("MONGODB_URI")


def require_mongo():
    if not os.environ.get("MONGODB_URI"):
        pytest.skip("MONGODB_URI not set; skipping Atlas integration test")
