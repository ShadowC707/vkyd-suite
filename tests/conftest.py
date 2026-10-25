"""Pytest configuration and shared test fixtures."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import Database, get_db
from app.main import app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def test_db(tmp_path: Path) -> Database:
    db_file = tmp_path / "test_vkyd.db"
    db = Database(db_file)
    db.run_migrations()
    return db


@pytest.fixture
def client(test_db: Database) -> TestClient:
    app.dependency_overrides[get_db] = lambda: test_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def chat_a_bytes() -> bytes:
    return (FIXTURES_DIR / "synthetic_chat_a.json").read_bytes()


@pytest.fixture
def chat_b_bytes() -> bytes:
    return (FIXTURES_DIR / "synthetic_chat_b.json").read_bytes()


@pytest.fixture
def account_export_bytes() -> bytes:
    return (FIXTURES_DIR / "account_export.json").read_bytes()


@pytest.fixture
def duplicate_ids_bytes() -> bytes:
    return (FIXTURES_DIR / "duplicate_ids_export.json").read_bytes()


@pytest.fixture
def malformed_bytes() -> bytes:
    return (FIXTURES_DIR / "malformed.json").read_bytes()


@pytest.fixture
def media_only_bytes() -> bytes:
    return (FIXTURES_DIR / "media_only_chat.json").read_bytes()
