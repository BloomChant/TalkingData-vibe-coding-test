import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture
def database(tmp_path, monkeypatch):
    from server.app import storage
    monkeypatch.setattr(storage, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(storage, 'DATABASE', tmp_path / 'test.sqlite3')
    monkeypatch.setattr(storage, 'STOP_BYTES', 18 * 1024**3)
    monkeypatch.setattr(storage, 'WARN_BYTES', 16 * 1024**3)
    storage.init()
    return storage


@pytest.fixture
def client(database):
    from fastapi.testclient import TestClient
    from server.app.main import app
    with TestClient(app) as test_client:
        yield test_client
