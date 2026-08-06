import os
import sys
import types
from datetime import datetime, timedelta

import mongomock
from werkzeug.security import check_password_hash, generate_password_hash

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app

GOOD_PASSWORD = 'NewPassw0rd!'


def build_client(monkeypatch):
    """App wired to an in-memory Mongo, with mail captured instead of sent."""
    sys.modules['app.pipeline.orchestrator'] = types.SimpleNamespace(run_pipeline=lambda *a, **k: None)
    sys.modules['app.pipeline.run_pex'] = types.SimpleNamespace(run_forecast=lambda *a, **k: None)
    sys.modules['app.pipeline.rust_runner'] = types.SimpleNamespace(run_rust_code=lambda *a, **k: None)
    sys.modules['app.pipeline.update_pex'] = types.SimpleNamespace(update_pex_generator=lambda *a, **k: None)

    mock_db = mongomock.MongoClient()['test-db']
    monkeypatch.setattr('app.routes.db', mock_db)

    sent = []
    monkeypatch.setattr('app.routes.send_email', lambda to, subject, body: sent.append((to, subject, body)) or True)

    mock_db.users.insert_one({
        'full_name': 'Test User',
        'email': 'user@example.com',
        'password_hash': generate_password_hash('OldPassw0rd!'),
    })

    return create_app().test_client(), mock_db, sent


def token_from_mail(sent):
    return sent[-1][2].split('token=')[1].split()[0]


def test_forgot_password_mails_a_link(monkeypatch):
    client, db, sent = build_client(monkeypatch)

    res = client.post('/api/forgot_password', json={'email': 'user@example.com'})

    assert res.status_code == 200
    assert len(sent) == 1
    assert sent[0][0] == 'user@example.com'
    assert '/reset-password?token=' in sent[0][2]

    stored = db.password_resets.find_one({'email': 'user@example.com'})
    assert stored is not None
    # The raw token must never be stored, only its hash.
    assert token_from_mail(sent) not in str(stored)


def test_unknown_email_is_indistinguishable_and_sends_nothing(monkeypatch):
    client, db, sent = build_client(monkeypatch)

    known = client.post('/api/forgot_password', json={'email': 'user@example.com'})
    unknown = client.post('/api/forgot_password', json={'email': 'nobody@example.com'})

    assert unknown.status_code == known.status_code
    assert unknown.get_json() == known.get_json()
    assert [to for to, _, _ in sent] == ['user@example.com']


def test_repeat_request_is_throttled(monkeypatch):
    client, db, sent = build_client(monkeypatch)

    client.post('/api/forgot_password', json={'email': 'user@example.com'})
    second = client.post('/api/forgot_password', json={'email': 'user@example.com'})

    assert second.status_code == 200
    assert len(sent) == 1, 'a second mail went out inside the resend window'


def test_reset_changes_password_and_burns_the_token(monkeypatch):
    client, db, sent = build_client(monkeypatch)

    client.post('/api/forgot_password', json={'email': 'user@example.com'})
    token = token_from_mail(sent)

    res = client.post('/api/reset_password', json={'token': token, 'password': GOOD_PASSWORD})
    assert res.status_code == 200

    user = db.users.find_one({'email': 'user@example.com'})
    assert check_password_hash(user['password_hash'], GOOD_PASSWORD)

    # Login with the new password works, the old one does not.
    assert client.post('/api/login', json={'email': 'user@example.com', 'password': GOOD_PASSWORD}).status_code == 200
    assert client.post('/api/login', json={'email': 'user@example.com', 'password': 'OldPassw0rd!'}).status_code == 401

    # Same token a second time is refused.
    again = client.post('/api/reset_password', json={'token': token, 'password': 'Another1Pass!'})
    assert again.status_code == 400


def test_expired_and_bogus_tokens_are_refused(monkeypatch):
    client, db, sent = build_client(monkeypatch)

    client.post('/api/forgot_password', json={'email': 'user@example.com'})
    token = token_from_mail(sent)

    db.password_resets.update_one(
        {'email': 'user@example.com'},
        {'$set': {'expires_at': datetime.utcnow() - timedelta(minutes=1)}},
    )

    assert client.post('/api/reset_password', json={'token': token, 'password': GOOD_PASSWORD}).status_code == 400
    assert client.post('/api/reset_password', json={'token': 'not-a-token', 'password': GOOD_PASSWORD}).status_code == 400

    user = db.users.find_one({'email': 'user@example.com'})
    assert check_password_hash(user['password_hash'], 'OldPassw0rd!'), 'password changed on a rejected reset'


def test_weak_password_is_rejected(monkeypatch):
    client, db, sent = build_client(monkeypatch)

    client.post('/api/forgot_password', json={'email': 'user@example.com'})
    token = token_from_mail(sent)

    res = client.post('/api/reset_password', json={'token': token, 'password': 'short'})
    assert res.status_code == 400

    # The token survives a rejected attempt so the user can try again.
    assert client.post('/api/reset_password', json={'token': token, 'password': GOOD_PASSWORD}).status_code == 200
