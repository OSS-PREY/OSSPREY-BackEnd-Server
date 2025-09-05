import os
import os
import os
from pathlib import Path
import sys
from datetime import datetime
import types
import mongomock

sys.path.append(str(Path(__file__).resolve().parent.parent))

sys.modules['app.pipeline.orchestrator'] = types.SimpleNamespace(run_pipeline=lambda *a, **k: None)
sys.modules['app.pipeline.run_pex'] = types.SimpleNamespace(run_forecast=lambda *a, **k: None)
sys.modules['app.pipeline.rust_runner'] = types.SimpleNamespace(run_rust_code=lambda *a, **k: None)
sys.modules['app.pipeline.update_pex'] = types.SimpleNamespace(update_pex_generator=lambda *a, **k: None)

from app import create_app


def setup_mock_db(monkeypatch):
    mock_client = mongomock.MongoClient()
    mock_db = mock_client['test-db']
    monkeypatch.setattr('app.routes.db', mock_db)
    return mock_db


def create_test_client():
    app = create_app()
    return app.test_client()


def test_track_login_records_event(monkeypatch):
    mock_db = setup_mock_db(monkeypatch)
    client = create_test_client()

    res = client.post('/api/track_login', json={'user_email': 'user@example.com'})
    assert res.status_code == 201

    stored = mock_db.login_tracking.find_one({'user_email': 'user@example.com'})
    assert stored is not None
    assert isinstance(stored['timestamp'], datetime)


def test_track_logout_records_event(monkeypatch):
    mock_db = setup_mock_db(monkeypatch)
    client = create_test_client()

    res = client.post('/api/track_logout', json={'user_email': 'user@example.com'})
    assert res.status_code == 201

    stored = mock_db.logout_tracking.find_one({'user_email': 'user@example.com'})
    assert stored is not None
    assert isinstance(stored['timestamp'], datetime)
