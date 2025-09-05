import os
import sys
import types
from datetime import datetime
import mongomock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app


def setup_mock_db(monkeypatch):
    mock_client = mongomock.MongoClient()
    mock_db = mock_client['test-db']
    monkeypatch.setattr('app.routes.db', mock_db)
    return mock_db


def create_test_client():
    app = create_app()
    return app.test_client()


def patch_pipelines():
    sys.modules['app.pipeline.orchestrator'] = types.SimpleNamespace(run_pipeline=lambda *a, **k: None)
    sys.modules['app.pipeline.run_pex'] = types.SimpleNamespace(run_forecast=lambda *a, **k: None)
    sys.modules['app.pipeline.rust_runner'] = types.SimpleNamespace(run_rust_code=lambda *a, **k: None)
    sys.modules['app.pipeline.update_pex'] = types.SimpleNamespace(update_pex_generator=lambda *a, **k: None)


def test_record_view_adds_timestamp(monkeypatch):
    patch_pipelines()
    mock_db = setup_mock_db(monkeypatch)
    client = create_test_client()

    before = datetime.utcnow()
    res = client.post('/api/record_view')
    after = datetime.utcnow()

    assert res.status_code == 201
    data = res.get_json()
    assert 'timestamp' in data

    stored = mock_db.view_timestamps.find_one()
    assert stored is not None
    assert before <= stored['timestamp'] <= after


def test_get_view_count_returns_total(monkeypatch):
    patch_pipelines()
    mock_db = setup_mock_db(monkeypatch)
    client = create_test_client()

    mock_db.view_timestamps.insert_many([
        {'timestamp': datetime.utcnow()},
        {'timestamp': datetime.utcnow()},
        {'timestamp': datetime.utcnow()},
    ])

    res = client.get('/api/view_count')
    assert res.status_code == 200
    data = res.get_json()
    assert data['count'] == 3
