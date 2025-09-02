import os
import os
import sys
import types
from datetime import datetime
import mongomock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app


def test_process_repo_saves_request(monkeypatch):
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
        'user_email': 'user@example.com',
        'github_repo': 'https://github.com/org/repo',
        'timestamp': '2024-06-12T15:32:00Z'
    }

    res = client.post('/api/process_repo', json=payload)
    assert res.status_code == 201

    stored = mock_db.user_repo_requests.find_one({'user_email': 'user@example.com'})
    assert stored is not None
    assert stored['github_repo'] == payload['github_repo']
    expected_ts = datetime.fromisoformat(payload['timestamp'].replace('Z', '+00:00'))
    assert stored['timestamp'] == expected_ts.replace(tzinfo=None)

