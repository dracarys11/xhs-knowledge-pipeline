"""Shared test fixtures."""

from pathlib import Path

import pytest

from xhs_ingest.state import StateStore


@pytest.fixture()
def store(tmp_path: Path):
    s = StateStore(tmp_path / "sync.db")
    yield s
    s.close()
