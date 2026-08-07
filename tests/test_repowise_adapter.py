"""Chat adapter tests. These never touch the real RepoWise -- requests is mocked."""
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repowise import project_id_from_github_url, repowise_bp


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def client():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(repowise_bp)

    return app.test_client()


@pytest.mark.parametrize('url', [
    'https://github.com/Owner/Repo',
    'https://github.com/Owner/Repo.git',
    'https://github.com/Owner/Repo/',
    'git@github.com:Owner/Repo.git',
    'Owner/Repo',
    '  https://github.com/Owner/Repo  ',
])
def test_project_id_matches_repowise_rule(url):
    assert project_id_from_github_url(url) == 'owner-repo'


@pytest.mark.parametrize('url', ['', None, 'not a url', 'https://gitlab.com/a/b'])
def test_project_id_rejects_garbage(url):
    with pytest.raises(ValueError):
        project_id_from_github_url(url)


def test_session_rejects_bad_url(client):
    assert client.post('/api/chat/session', json={'github_url': 'nope'}).status_code == 400


def test_message_rejects_empty_query(client):
    assert client.post('/api/chat/message', json={'project_id': 'a-b', 'query': '  '}).status_code == 400


def test_unreachable_repowise_is_a_friendly_503(client):
    with patch('app.repowise.requests.request', side_effect=requests.ConnectionError('refused')):
        r = client.post('/api/chat/message', json={'project_id': 'a-b', 'query': 'hi'})
    assert r.status_code == 503
    assert 'unavailable' in r.get_json()['message'].lower()


def test_session_indexes_when_project_is_missing(client):
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, url))
        if method == 'GET':
            return FakeResponse(404, {'detail': 'Project not found'})

        return FakeResponse(200, {'status': 'indexing_complete'})

    with patch('app.repowise.requests.request', side_effect=fake):
        r = client.post('/api/chat/session', json={'github_url': 'https://github.com/a/b'})

    assert r.get_json() == {'status': 'ready', 'project_id': 'a-b', 'indexed_now': True}
    assert calls[1][1].endswith('/api/projects/add')


def test_session_is_fast_path_when_already_indexed(client):
    with patch('app.repowise.requests.request',
               return_value=FakeResponse(200, {'id': 'a-b', 'indexed': True})) as m:
        r = client.post('/api/chat/session', json={'github_url': 'https://github.com/a/b'})

    assert r.get_json()['indexed_now'] is False
    assert m.call_count == 1  # no add call


def test_conversation_state_round_trips_verbatim(client):
    state_in = {'running_summary': 'so far', 'turn_count': 1, 'project_id': 'a-b'}
    state_out = {'running_summary': 'updated', 'turn_count': 2, 'project_id': 'a-b'}
    sent = {}

    def fake(method, url, **kwargs):
        sent.update(kwargs.get('json') or {})

        return FakeResponse(200, {
            'response': 'an answer',
            'suggested_questions': ['q1'],
            'conversation_state': state_out,
            'sources': [],
        })

    with patch('app.repowise.requests.request', side_effect=fake):
        r = client.post('/api/chat/message',
                        json={'project_id': 'a-b', 'query': 'why?', 'conversation_state': state_in})

    assert sent['conversation_state'] == state_in
    assert r.get_json()['conversation_state'] == state_out
    assert r.get_json()['response'] == 'an answer'


def test_upstream_error_is_not_leaked(client):
    with patch('app.repowise.requests.request',
               return_value=FakeResponse(500, {'detail': 'chromadb exploded'})):
        r = client.post('/api/chat/message', json={'project_id': 'a-b', 'query': 'hi'})

    assert r.status_code == 502
    assert 'chromadb' not in r.get_json()['message']
