import os
import sys
import mongomock
from datetime import datetime

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


def test_get_all_users_returns_users(monkeypatch):
    mock_db = setup_mock_db(monkeypatch)
    client = create_test_client()

    mock_db.users.insert_many([
        {
            'full_name': 'Jane Doe',
            'email': 'jane@example.com',
            'affiliation': 'UC Davis',
            'referral': 'Conf',
            'created_at': datetime.utcnow(),
            'password_hash': 'hash',
        },
        {
            'full_name': 'John Doe',
            'email': 'john@example.com',
            'affiliation': 'UC Davis',
            'referral': 'Friend',
            'created_at': datetime.utcnow(),
            'password_hash': 'hash',
        },
    ])

    res = client.get('/api/users')
    assert res.status_code == 200
    data = res.get_json()
    assert len(data['users']) == 2
    emails = {u['email'] for u in data['users']}
    assert emails == {'jane@example.com', 'john@example.com'}
    # Ensure password hash is not included
    assert all('password_hash' not in u for u in data['users'])


def test_get_user_repositories_returns_repos(monkeypatch):
    mock_db = setup_mock_db(monkeypatch)
    client = create_test_client()

    mock_db.user_repo_requests.insert_many([
        {
            'user_email': 'user@example.com',
            'github_repo': 'https://github.com/org/repo1',
            'timestamp': datetime.utcnow(),
        },
        {
            'user_email': 'user@example.com',
            'github_repo': 'https://github.com/org/repo2',
            'timestamp': datetime.utcnow(),
        },
        {
            'user_email': 'user@example.com',
            'github_repo': 'https://github.com/org/repo1',
            'timestamp': datetime.utcnow(),
        },
        {
            'user_email': 'other@example.com',
            'github_repo': 'https://github.com/org/repo3',
            'timestamp': datetime.utcnow(),
        },
    ])

    res = client.get('/api/user_repositories', query_string={'email': 'user@example.com'})
    assert res.status_code == 200
    data = res.get_json()
    assert set(data['repositories']) == {
        'https://github.com/org/repo1',
        'https://github.com/org/repo2',
    }

