"""Use disposable databases on a real MongoDB instance for integration tests."""
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main as m


@pytest.fixture
def startup_dir():
    # Keep temporary checkouts in the ignored workspace directory, so the test
    # also works on machines whose shared OS temporary folder is restricted.
    runtime = Path(__file__).resolve().parents[1] / ".runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="startup-", dir=runtime) as directory:
        yield Path(directory)


@pytest.fixture
def api(monkeypatch):
    connection = MongoClient(os.getenv("TEST_MONGO_URI", "mongodb://localhost:27018"),
                             tz_aware=True, serverSelectionTimeoutMS=2000)
    connection.admin.command("ping")
    database = connection["hrone_test_" + uuid4().hex]
    monkeypatch.setattr(m, "db", database)
    monkeypatch.setattr(m, "client", connection)
    with TestClient(m.app) as http:
        try:
            yield http
        finally:
            connection.drop_database(database.name)
