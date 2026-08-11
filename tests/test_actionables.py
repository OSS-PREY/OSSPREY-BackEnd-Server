"""Project-specific actionable selection.

The model and the embedding service are always mocked. What matters here is the
boundary either side of them: which categories a project's numbers argue for,
that the scoring cannot be dominated by one term, and that whatever the model
replies is filtered back down to ids that actually exist.
"""
from unittest.mock import patch

import numpy as np
import pytest
from flask import Flask

from app.actionables import (
    BOOST_CAP, CATEGORIES, CICD, COMMUNITY, DOCS, GOVERNANCE, ONBOARDING,
    SECURITY, STANDARDS, TESTING, actionables_bp, build_profile, parse_choices,
    wanted_categories,
)

DIGEST = {
    'month': 12,
    'forecast': {'series': [{'month': 9, 'value': 0.61}, {'month': 12, 'value': 0.41}]},
    'technical': {
        'series': {'developers': [{'month': 9, 'value': 31}, {'month': 12, 'value': 14}]},
        'top_contributor_share': 0.38,
        'solo_file_types': {'count': 212, 'total': 340},
    },
    'social': {
        'series': {'participants': [{'month': 9, 'value': 22}, {'month': 12, 'value': 8}]},
        'silent_developers': {'count': 9, 'total': 14},
    },
    'metadata': {'languages': ['C++', 'Python']},
}


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(actionables_bp)

    return app.test_client()


class TestWantedCategories:
    def test_missing_documents_pick_their_categories(self):
        wanted = wanted_categories({}, ['CONTRIBUTING', 'SECURITY'], has_ci=True)

        assert ONBOARDING in wanted
        assert DOCS in wanted
        assert SECURITY in wanted

    def test_no_ci_asks_for_ci(self):
        assert CICD in wanted_categories({}, [], has_ci=False)
        assert CICD not in wanted_categories({}, [], has_ci=True)

    def test_knowledge_silos_ask_for_code_standards(self):
        # 212 of 340 files touched by one person.
        assert STANDARDS in wanted_categories(DIGEST, [], has_ci=True)

    def test_concentrated_work_asks_for_governance(self):
        assert GOVERNANCE in wanted_categories(DIGEST, [], has_ci=True)

    def test_silent_committers_ask_for_community(self):
        assert COMMUNITY in wanted_categories(DIGEST, [], has_ci=True)

    def test_a_healthy_project_still_gets_a_shortlist(self):
        # No weak signal at all must not mean an empty panel.
        wanted = wanted_categories({'technical': {}, 'social': {}}, [], has_ci=True)

        assert wanted == {TESTING, STANDARDS}

    def test_survives_a_hostile_digest(self):
        # The digest is built in the browser, so it is untrusted.
        assert wanted_categories({'technical': 'nonsense', 'social': None},
                                 None, has_ci=False)

    def test_every_bit_maps_to_a_real_category(self):
        for bit in wanted_categories(DIGEST, ['SECURITY'], has_ci=False):
            assert CATEGORIES[bit]


class TestProfile:
    def test_splits_need_from_identity(self):
        need, domain = build_profile(DIGEST, 'gem5', {'description': 'a simulator'},
                                     ['SECURITY'], '')

        # The numbers belong to the need query...
        assert '38%' in need
        assert 'SECURITY' in need
        # ...and what the project IS belongs to the other, which is what stops a
        # C++ project being matched to another language's tooling.
        assert 'C++' in domain
        assert 'a simulator' in domain

    def test_domain_survives_missing_metadata(self):
        need, domain = build_profile({}, 'x', {}, [], '')

        assert need and domain


class TestParseChoices:
    def test_reads_the_id_and_the_reason(self):
        text = '[12] because contributors fell\n[7] because no code of conduct'

        assert parse_choices(text, {7, 12}) == [
            (12, 'because contributors fell'), (7, 'because no code of conduct')]

    def test_drops_ids_that_were_never_offered(self):
        # The model invents indices; they would IndexError against the catalog.
        assert parse_choices('[99] made up\n[3] real', {3}) == [(3, 'real')]

    def test_drops_a_choice_with_no_reason(self):
        assert parse_choices('[3]\n[4] a real reason', {3, 4}) == [(4, 'a real reason')]

    def test_deduplicates(self):
        assert parse_choices('[3] one\n[3] again', {3}) == [(3, 'one')]

    def test_caps_at_ten(self):
        text = '\n'.join(f'[{i}] reason {i}' for i in range(30))

        assert len(parse_choices(text, set(range(30)))) == 10

    def test_empty_when_the_model_writes_prose(self):
        assert parse_choices('I think you should improve testing.', {1}) == []
        assert parse_choices(None, {1}) == []


class TestScoring:
    """The boost orders near-ties; it must never decide the ranking.

    Measured before this was capped: a multiplicative 1.15-per-category boost
    reached 2.7x on a project matching seven categories and swamped the cosine
    entirely -- gem5 and a Python RAG tool came out 0.67 alike.
    """

    def test_boost_cannot_outweigh_relevance(self):
        from app.actionables import BOOST_PER_CATEGORY

        # Worst case: every category matches.
        assert BOOST_PER_CATEGORY * BOOST_CAP < 0.15

    def test_retrieval_blends_need_and_domain(self):
        from app.actionables import W_DOMAIN, W_NEED

        assert W_NEED + W_DOMAIN == pytest.approx(1.0)
        # Domain must carry real weight or unlike projects converge.
        assert W_DOMAIN >= 0.3


class TestRoute:
    def test_rejects_an_empty_digest(self, client):
        assert client.post('/api/actionables', json={'digest': {}}).status_code == 400
        assert client.post('/api/actionables', json={}).status_code == 400

    def test_returns_the_reranked_choice(self, client):
        catalog = [{'title': f'entry {i}', 'importance': 1} for i in range(50)]
        index = {
            'vectors': np.zeros((50, 4), dtype=np.float32),
            'category': np.zeros(50, dtype=np.uint8),
            'importance': np.zeros(50, dtype=np.float32),
            'confidence': np.zeros(50, dtype=np.float32),
            'catalog': catalog,
        }

        with patch('app.actionables.load_index', return_value=index), \
             patch('app.actionables.fetch_documents', return_value=('', [], ['SECURITY'])), \
             patch('app.actionables.retrieve', return_value=[(3, 0.9), (4, 0.8)]), \
             patch('app.actionables._generate', return_value='[3] fits because of X'):
            response = client.post('/api/actionables',
                                   json={'digest': DIGEST, 'project_name': 'gem5',
                                         'refresh': True})

        body = response.get_json()

        assert response.status_code == 200
        assert body['reranked'] is True
        assert [a['catalog_index'] for a in body['actionables']] == [3]
        assert body['actionables'][0]['why'] == 'fits because of X'

    def test_falls_back_to_retrieval_when_the_model_is_down(self, client):
        # Retrieval alone is still project-specific -- better than the
        # importance sort the panel used to show.
        from app.repowise import RepoWiseUnavailable

        catalog = [{'title': f'entry {i}'} for i in range(50)]
        index = {'vectors': np.zeros((50, 4), dtype=np.float32),
                 'category': np.zeros(50, dtype=np.uint8),
                 'importance': np.zeros(50, dtype=np.float32),
                 'confidence': np.zeros(50, dtype=np.float32),
                 'catalog': catalog}

        with patch('app.actionables.load_index', return_value=index), \
             patch('app.actionables.fetch_documents', return_value=('', [], [])), \
             patch('app.actionables.retrieve', return_value=[(1, 0.9), (2, 0.8)]), \
             patch('app.actionables._generate', side_effect=RepoWiseUnavailable('down')):
            response = client.post('/api/actionables',
                                   json={'digest': DIGEST, 'refresh': True})

        body = response.get_json()

        assert response.status_code == 200
        assert body['reranked'] is False
        assert len(body['actionables']) == 2

    def test_missing_index_is_a_503_not_a_crash(self, client):
        from app.actionables import IndexMissing

        with patch('app.actionables.load_index', side_effect=IndexMissing('no file')):
            response = client.post('/api/actionables', json={'digest': DIGEST})

        assert response.status_code == 503
