import os
import sys
from datetime import datetime
import mongomock
import types

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app


def test_register_user_saves_timestamp(monkeypatch):
    # Patch out unused pipeline imports
    sys.modules['app.pipeline.orchestrator'] = types.SimpleNamespace(run_pipeline=lambda *a, **k: None)
    sys.modules['app.pipeline.run_pex'] = types.SimpleNamespace(run_forecast=lambda *a, **k: None)
    sys.modules['app.pipeline.rust_runner'] = types.SimpleNamespace(run_rust_code=lambda *a, **k: None)
    sys.modules['app.pipeline.update_pex'] = types.SimpleNamespace(update_pex_generator=lambda *a, **k: None)

    mock_client = mongomock.MongoClient()
    mock_db = mock_client['test-db']
    monkeypatch.setattr('app.routes.db', mock_db)

    app = create_app()
    client = app.test_client()

    payload = {
        'full_name': 'Test User',
        'email': 'test@example.com',
        'affiliation': 'Test Org',
        'password': 'secret',
        'referral': 'Friend'
    }

    before = datetime.utcnow()
    res = client.post('/api/register', json=payload)
    after = datetime.utcnow()

    assert res.status_code == 201

    stored = mock_db.users.find_one({'email': payload['email']})
    assert stored is not None
    assert 'registered_at' in stored
    assert before <= stored['registered_at'] <= after
