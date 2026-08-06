import os
import sys
import time
import types
import mongomock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.services.queue_manager import QueueManager


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def patch_pipelines():
    sys.modules['app.pipeline.orchestrator'] = types.SimpleNamespace(run_pipeline=lambda *a, **k: None)
    sys.modules['app.pipeline.run_pex'] = types.SimpleNamespace(run_forecast=lambda *a, **k: None)
    sys.modules['app.pipeline.rust_runner'] = types.SimpleNamespace(run_rust_code=lambda *a, **k: None)
    sys.modules['app.pipeline.update_pex'] = types.SimpleNamespace(update_pex_generator=lambda *a, **k: None)


def setup_mock_db(monkeypatch):
    mock_client = mongomock.MongoClient()
    mock_db = mock_client['test-db']
    monkeypatch.setattr('app.routes.db', mock_db)
    return mock_db


def install_fake_queue(monkeypatch, worker):
    """Swap the module-level queue for one backed by a fast fake worker."""
    qm = QueueManager(
        worker=worker,
        max_concurrent=2,
        default_job_seconds=1,
        result_is_error=lambda r: isinstance(r, dict) and bool(r.get('error')),
    )
    monkeypatch.setattr('app.routes.pipeline_queue', qm)
    return qm


def test_upload_enqueues_and_completes(monkeypatch):
    patch_pipelines()
    setup_mock_db(monkeypatch)

    def fake_pipeline(git_link):
        return {"git_link": git_link, "metadata": {"name": "repo"}}

    install_fake_queue(monkeypatch, fake_pipeline)

    client = create_app().test_client()

    res = client.post('/api/upload_git_link', json={'git_link': 'https://github.com/o/r.git'})
    assert res.status_code == 202
    job = res.get_json()
    assert 'job_id' in job
    assert job['status'] in ('queued', 'running')
    assert 'estimated_wait_seconds' in job
    assert 'position' in job

    job_id = job['job_id']
    assert wait_for(
        lambda: client.get(f'/api/queue_status/{job_id}').get_json()['status'] == 'completed'
    )
    final = client.get(f'/api/queue_status/{job_id}').get_json()
    assert final['result']['git_link'] == 'https://github.com/o/r.git'


def test_upload_marks_pipeline_error_result_as_failed(monkeypatch):
    patch_pipelines()
    setup_mock_db(monkeypatch)

    def failing_pipeline(git_link):
        return {"error": "GitHub scraping failed: Repository is private!"}

    install_fake_queue(monkeypatch, failing_pipeline)
    client = create_app().test_client()

    res = client.post('/api/upload_git_link', json={'git_link': 'https://github.com/o/private.git'})
    job_id = res.get_json()['job_id']

    assert wait_for(
        lambda: client.get(f'/api/queue_status/{job_id}').get_json()['status'] == 'failed'
    )
    final = client.get(f'/api/queue_status/{job_id}').get_json()
    assert 'private' in final['error'].lower()


def test_upload_rejects_non_git_link(monkeypatch):
    patch_pipelines()
    setup_mock_db(monkeypatch)
    client = create_app().test_client()

    res = client.post('/api/upload_git_link', json={'git_link': 'https://github.com/o/r'})
    assert res.status_code == 400


def test_upload_rejects_empty_link(monkeypatch):
    patch_pipelines()
    setup_mock_db(monkeypatch)
    client = create_app().test_client()

    res = client.post('/api/upload_git_link', json={})
    assert res.status_code == 400


def test_queue_status_unknown_returns_404(monkeypatch):
    patch_pipelines()
    setup_mock_db(monkeypatch)
    client = create_app().test_client()

    res = client.get('/api/queue_status/does-not-exist')
    assert res.status_code == 404


def test_queue_stats_and_cancel_unknown(monkeypatch):
    patch_pipelines()
    setup_mock_db(monkeypatch)
    client = create_app().test_client()

    stats = client.get('/api/queue_stats')
    assert stats.status_code == 200
    assert 'max_concurrent' in stats.get_json()

    assert client.post('/api/cancel_job/nope').status_code == 404
