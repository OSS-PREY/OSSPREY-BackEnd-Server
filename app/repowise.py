"""Chat backend: a thin adapter over the RepoWise service running on this host.

The browser only ever talks to ``/api/chat/*``. This module is the single place
that knows RepoWise's URL, payload shapes and quirks -- if RepoWise changes,
only this file does.

Stateless by design: the conversation state round-trips through the client, so
nothing here assumes a particular gunicorn worker served the previous turn.
"""
import logging
import os
import re

import requests
from flask import Blueprint, jsonify, request
from flask_cors import cross_origin

logger = logging.getLogger(__name__)

repowise_bp = Blueprint('repowise', __name__)

BASE_URL = os.environ.get('REPOWISE_BASE_URL', 'http://localhost:8000').rstrip('/')

CONNECT_TIMEOUT = 5
QUERY_TIMEOUT = 120     # Ollama on CPU is slow.
INDEX_TIMEOUT = 600     # First-time indexing crawls GitHub and re-embeds docs.
MAX_QUERY_CHARS = 4000

# Mirrors RepoWise's own parse_github_url(); the project id it derives is
# f"{owner}-{repo}".lower(), so ours has to agree character for character.
_URL_PATTERNS = (
    r'https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$',
    r'git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$',
    r'^([^/]+)/([^/]+)$',
)


class RepoWiseUnavailable(Exception):
    """RepoWise could not be reached (connection refused, timeout, ...)."""


def project_id_from_github_url(url):
    """``https://github.com/OWNER/repo.git`` -> ``owner-repo``."""
    for pattern in _URL_PATTERNS:
        match = re.match(pattern, (url or '').strip())
        if match:
            owner, repo = match.groups()
            return f"{owner}-{repo.replace('.git', '')}".lower()
    raise ValueError(f'Invalid GitHub URL: {url}')


def _call(method, path, read_timeout, **kwargs):
    url = f'{BASE_URL}{path}'
    try:
        return requests.request(method, url, timeout=(CONNECT_TIMEOUT, read_timeout), **kwargs)
    except requests.RequestException as e:
        logger.error(f'RepoWise call failed: {method} {url}: {e}')
        raise RepoWiseUnavailable(str(e))


def _json(response):
    try:
        return response.json()
    except ValueError:
        return {}


@repowise_bp.errorhandler(RepoWiseUnavailable)
def _handle_unavailable(_e):
    return jsonify({'message': 'The assistant is temporarily unavailable. Please try again shortly.'}), 503


@repowise_bp.route('/api/chat/health', methods=['GET'])
@cross_origin(origin='*')
def chat_health():
    response = _call('GET', '/api/system-status', QUERY_TIMEOUT)
    data = _json(response)
    if response.status_code != 200:
        return jsonify({'status': 'unavailable', 'ollama': False, 'projects_indexed': 0}), 200

    return jsonify({
        'status': 'ready' if data.get('llm', {}).get('available') else 'unavailable',
        'ollama': bool(data.get('llm', {}).get('available')),
        'projects_indexed': data.get('rag', {}).get('projects_indexed', 0),
    }), 200


@repowise_bp.route('/api/chat/session', methods=['POST'])
@cross_origin(origin='*')
def chat_session():
    """Make sure the repo is indexed, and hand back the id used for messages.

    Already-indexed repos return immediately; a new one blocks here while
    RepoWise crawls and embeds it (minutes), which is why the frontend shows a
    "preparing context" state.
    """
    payload = request.get_json(silent=True) or {}
    try:
        project_id = project_id_from_github_url(payload.get('github_url'))
    except ValueError:
        return jsonify({'message': 'That does not look like a GitHub repository URL.'}), 400

    response = _call('GET', f'/api/projects/{project_id}', QUERY_TIMEOUT)
    if response.status_code == 200 and _json(response).get('indexed'):
        return jsonify({'status': 'ready', 'project_id': project_id, 'indexed_now': False}), 200

    response = _call('POST', '/api/projects/add', INDEX_TIMEOUT,
                     json={'github_url': payload.get('github_url')})
    if response.status_code >= 400:
        logger.error(f'RepoWise add failed for {project_id}: {response.status_code} {response.text[:500]}')
        return jsonify({'message': 'Could not prepare that repository for chat.'}), 502

    return jsonify({'status': 'ready', 'project_id': project_id, 'indexed_now': True}), 200


@repowise_bp.route('/api/chat/message', methods=['POST'])
@cross_origin(origin='*')
def chat_message():
    payload = request.get_json(silent=True) or {}
    query = (payload.get('query') or '').strip()[:MAX_QUERY_CHARS]
    project_id = payload.get('project_id')
    if not query:
        return jsonify({'message': 'Please type a question first.'}), 400

    response = _call('POST', '/api/query', QUERY_TIMEOUT, json={
        'project_id': project_id,
        'query': query,
        'conversation_state': payload.get('conversation_state'),
        'max_results': 5,
        'temperature': 0,
        'stream': False,
    })
    if response.status_code >= 400:
        logger.error(f'RepoWise query failed for {project_id}: {response.status_code} {response.text[:500]}')
        return jsonify({'message': 'The assistant could not answer that just now.'}), 502

    data = _json(response)

    return jsonify({
        'response': data.get('response', ''),
        'suggested_questions': data.get('suggested_questions', []),
        'conversation_state': data.get('conversation_state'),
        'sources': data.get('sources', []),
    }), 200
