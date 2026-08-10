"""The pain points pipeline: evidence rendering, bullet parsing, and the route.

The LLM is always mocked. What is worth testing here is the boundary either side
of it -- that untrusted digest values cannot break the prompt, and that whatever
prose the model wraps its bullets in gets stripped.
"""
from unittest.mock import patch

import pytest
from flask import Flask

from app.pain_points import build_evidence, build_prompt, pain_points_bp, to_bullets


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(pain_points_bp)

    return app.test_client()


DIGEST = {
    'month': 12,
    'forecast': {'series': [{'month': 10, 'value': 0.55}, {'month': 12, 'value': 0.41}],
                 'latest': 0.41},
    'technical': {
        'series': {'developers': [{'month': 10, 'value': 24}, {'month': 12, 'value': 14}]},
        'top_contributor_share': 0.38,
        'solo_files': {'count': 212, 'total': 340},
    },
    'social': {
        'series': {'participants': [{'month': 10, 'value': 17}, {'month': 12, 'value': 8}]},
        'silent_developers': {'count': 9, 'total': 14},
    },
}


class TestEvidence:
    def test_renders_every_signal(self):
        text = build_evidence(DIGEST, 'gem5')

        assert 'gem5' in text
        assert 'm12=0.41' in text
        assert '38%' in text
        assert '212 of 340' in text
        assert 'silent committers: 9 of the 14' in text

    def test_states_the_direction_of_travel(self):
        # A bare list of numbers makes the model do arithmetic it gets wrong.
        assert 'down 25%' in build_evidence(DIGEST, 'gem5')

    def test_survives_a_hostile_digest(self):
        # The digest is built in the browser, so it is untrusted input.
        junk = {
            'forecast': {'series': ['not a dict', None], 'latest': 'NaN'},
            'technical': {'series': {'developers': None}, 'solo_files': {'total': 'x'}},
            'social': 'not a dict',
        }

        assert isinstance(build_evidence(junk, 'x'), str)

    def test_is_bounded(self):
        huge = {'technical': {'series': {'developers':
                [{'month': i, 'value': i} for i in range(10000)]}}}

        assert len(build_evidence(huge, 'x')) < 7000

    def test_omits_sections_with_no_data(self):
        assert 'SOCIAL NETWORK' not in build_evidence({'forecast': {'latest': 0.4}}, 'x')


class TestPrompt:
    def test_forbids_recommendations(self):
        # Pain points and actionables are different panels; the model drifts
        # into proposing fixes unless told not to.
        prompt = build_prompt('evidence', 'docs', [], 'gem5')

        assert 'not a solution' in prompt.lower()
        assert 'do not recommend' in prompt.lower()

    def test_forbids_invented_urls(self):
        # Measured failure: the model cited
        # https://github.com/gem5/gem5/blob/master/docs/contributing.md, which
        # does not exist.
        assert 'Never write a URL' in build_prompt('e', 'd', [], 'p')

    def test_names_the_documents_that_are_missing(self):
        prompt = build_prompt('e', 'd', ['SECURITY', 'CODE_OF_CONDUCT'], 'p')

        assert 'NOT PRESENT' in prompt
        assert 'SECURITY' in prompt

    def test_says_so_when_no_documents_were_retrieved(self):
        assert 'none were retrieved' in build_prompt('e', '', [], 'p')


class TestBullets:
    def test_strips_the_preamble(self):
        text = 'Here are the pain points:\n\n- Contributors fell 55%\n- Nobody reviews'

        assert to_bullets(text) == ['- Contributors fell 55%', '- Nobody reviews']

    def test_accepts_numbered_and_starred_lists(self):
        assert to_bullets('1. First\n* Second\n• Third') == ['- First', '- Second', '- Third']

    def test_caps_the_count(self):
        assert len(to_bullets('\n'.join(f'- item {i}' for i in range(40)))) == 8

    def test_empty_when_the_model_answers_in_prose(self):
        assert to_bullets('This project looks healthy overall.') == []

    def test_handles_no_answer(self):
        assert to_bullets('') == []
        assert to_bullets(None) == []


class TestRoute:
    def test_rejects_an_empty_digest(self, client):
        assert client.post('/api/pain-points', json={'digest': {}}).status_code == 400
        assert client.post('/api/pain-points', json={}).status_code == 400

    def test_returns_bullets(self, client):
        with patch('app.pain_points.fetch_documents', return_value=('docs', [], [])), \
             patch('app.pain_points._generate', return_value='- Contributors fell 55%'):
            response = client.post('/api/pain-points',
                                   json={'digest': DIGEST, 'project_name': 'gem5'})

        assert response.status_code == 200
        assert response.get_json()['pain_points'] == ['- Contributors fell 55%']

    def test_a_healthy_project_is_not_an_error(self, client):
        with patch('app.pain_points.fetch_documents', return_value=('', [], [])), \
             patch('app.pain_points._generate', return_value='Nothing looks wrong.'):
            response = client.post('/api/pain-points', json={'digest': DIGEST})

        assert response.status_code == 200
        assert response.get_json()['pain_points'] == []

    def test_analyses_without_documents(self, client):
        # Apache and Eclipse projects carry no GitHub URL, so RepoWise has
        # nothing indexed for them. The networks alone must still work.
        with patch('app.pain_points.requests.post', side_effect=Exception('no repowise')), \
             patch('app.pain_points._generate', return_value='- Contributors fell 55%'):
            response = client.post('/api/pain-points', json={'digest': DIGEST})

        assert response.status_code == 200
        assert response.get_json()['pain_points']

    def test_unreachable_llm_is_a_friendly_503(self, client):
        import requests as rq

        with patch('app.pain_points.fetch_documents', return_value=('', [], [])), \
             patch('app.pain_points.requests.post', side_effect=rq.ConnectionError('refused')):
            response = client.post('/api/pain-points', json={'digest': DIGEST})

        assert response.status_code == 503
        assert 'unavailable' in response.get_json()['message'].lower()
