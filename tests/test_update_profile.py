import os
import sys
import types

import mongomock
from werkzeug.security import generate_password_hash

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app

PASSWORD = 'OldPassw0rd!'


def build_client(monkeypatch):
    sys.modules['app.pipeline.orchestrator'] = types.SimpleNamespace(run_pipeline=lambda *a, **k: None)
    sys.modules['app.pipeline.run_pex'] = types.SimpleNamespace(run_forecast=lambda *a, **k: None)
    sys.modules['app.pipeline.rust_runner'] = types.SimpleNamespace(run_rust_code=lambda *a, **k: None)
    sys.modules['app.pipeline.update_pex'] = types.SimpleNamespace(update_pex_generator=lambda *a, **k: None)

    mock_db = mongomock.MongoClient()['test-db']
    monkeypatch.setattr('app.routes.db', mock_db)

    for email in ('user@example.com', 'other@example.com'):
        mock_db.users.insert_one({
            'full_name': 'Test User',
            'email': email,
            'affiliation': 'Old Org',
            'password_hash': generate_password_hash(PASSWORD),
        })

    return create_app().test_client(), mock_db


def login(client, email='user@example.com'):
    res = client.post('/api/login', json={'email': email, 'password': PASSWORD})
    assert res.status_code == 200

    return res.get_json()


def auth(token):
    return {'Authorization': f'Bearer {token}'}


def test_login_returns_the_full_profile(monkeypatch):
    client, db = build_client(monkeypatch)

    body = login(client)

    assert body['access_token']
    assert body['user'] == {
        'email': 'user@example.com',
        'name': 'Test User',
        'affiliation': 'Old Org',
        'role': '',
    }


def test_update_changes_name_affiliation_and_role(monkeypatch):
    client, db = build_client(monkeypatch)
    token = login(client)['access_token']

    res = client.post('/api/update_profile', headers=auth(token), json={
        'name': 'New Name',
        'affiliation': 'New Org',
        'role': 'Researcher',
        'email': 'user@example.com',
    })

    assert res.status_code == 200
    assert res.get_json()['user'] == {
        'email': 'user@example.com',
        'name': 'New Name',
        'affiliation': 'New Org',
        'role': 'Researcher',
    }

    stored = db.users.find_one({'email': 'user@example.com'})
    assert stored['full_name'] == 'New Name'
    assert stored['affiliation'] == 'New Org'
    assert stored['role'] == 'Researcher'

    # The change survives a fresh sign-in.
    assert login(client)['user']['affiliation'] == 'New Org'


def test_email_is_never_changed(monkeypatch):
    client, db = build_client(monkeypatch)
    token = login(client)['access_token']

    res = client.post('/api/update_profile', headers=auth(token), json={
        'name': 'New Name',
        'affiliation': 'New Org',
        'email': 'attacker@example.com',
    })

    # A body email that disagrees with the token is refused outright.
    assert res.status_code == 403
    assert db.users.find_one({'email': 'attacker@example.com'}) is None

    stored = db.users.find_one({'email': 'user@example.com'})
    assert stored['full_name'] == 'Test User', 'profile changed on a refused request'

    # Even with no email in the body, the account keeps its address.
    res = client.post('/api/update_profile', headers=auth(token), json={
        'name': 'New Name',
        'affiliation': 'New Org',
    })
    assert res.status_code == 200
    assert res.get_json()['user']['email'] == 'user@example.com'
    assert db.users.count_documents({'email': 'user@example.com'}) == 1


def test_cannot_edit_another_account(monkeypatch):
    client, db = build_client(monkeypatch)
    token = login(client)['access_token']

    res = client.post('/api/update_profile', headers=auth(token), json={
        'name': 'Hijacked',
        'affiliation': 'Hijacked Org',
        'email': 'other@example.com',
    })

    assert res.status_code == 403

    victim = db.users.find_one({'email': 'other@example.com'})
    assert victim['full_name'] == 'Test User'
    assert victim['affiliation'] == 'Old Org'


def test_update_requires_a_token(monkeypatch):
    client, db = build_client(monkeypatch)

    res = client.post('/api/update_profile', json={
        'name': 'New Name',
        'affiliation': 'New Org',
        'email': 'user@example.com',
    })

    assert res.status_code == 401
    assert db.users.find_one({'email': 'user@example.com'})['full_name'] == 'Test User'


def test_required_fields_and_length_are_validated(monkeypatch):
    client, db = build_client(monkeypatch)
    token = login(client)['access_token']

    blank = client.post('/api/update_profile', headers=auth(token), json={'name': '  ', 'affiliation': 'Org'})
    assert blank.status_code == 400

    too_long = client.post('/api/update_profile', headers=auth(token), json={
        'name': 'x' * 201,
        'affiliation': 'Org',
    })
    assert too_long.status_code == 400

    assert db.users.find_one({'email': 'user@example.com'})['full_name'] == 'Test User'


def test_role_can_be_cleared(monkeypatch):
    client, db = build_client(monkeypatch)
    token = login(client)['access_token']

    client.post('/api/update_profile', headers=auth(token), json={
        'name': 'Test User', 'affiliation': 'Org', 'role': 'Researcher',
    })
    res = client.post('/api/update_profile', headers=auth(token), json={
        'name': 'Test User', 'affiliation': 'Org', 'role': '',
    })

    assert res.status_code == 200
    assert res.get_json()['user']['role'] == ''
