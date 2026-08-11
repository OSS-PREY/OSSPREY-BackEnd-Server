"""Project-specific actionable selection.

The panel used to show the same ten entries for every project, because the only
ranking applied was importance then confidence -- no project input at all. This
picks from the same 3837-entry catalog using what OSSPREY already measures about
the repository.

The naive way to do that is to ask the model "does this fit?" once per entry:
3837 calls. Instead the catalog is embedded offline (scripts/build_actionable_
index.py) and the 3837 comparisons happen as one matrix multiply, so a request
costs two model calls -- one to embed the project, one to rerank a shortlist.

Degrades in steps rather than failing: no LLM -> the retrieval order; no index
-> HTTP 503 and the frontend falls back to its own importance sort.
"""
import json
import logging
import os
import re

import numpy as np
import requests
from flask import Blueprint, jsonify, request
from flask_cors import cross_origin

from app.pain_points import (
    CONNECT_TIMEOUT, OLLAMA_URL, RepoWiseUnavailable, _generate, _num,
    build_evidence, fetch_documents, missing_documents,
)
from app.inference_queue import InferenceBusy, gpu_slot
from app.repowise import project_id_from_github_url

logger = logging.getLogger(__name__)

actionables_bp = Blueprint('actionables', __name__)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.environ.get(
    'ACTIONABLES_INDEX', os.path.join(REPO, 'data', 'actionables_index.npz'))
CATALOG_PATH = os.environ.get('ACTIONABLES_CATALOG', os.path.normpath(os.path.join(
    REPO, '..', 'OSSPREY-FrontEnd-Server', 'public', 'updated_react_set2.json')))

EMBED_MODEL = os.environ.get('OLLAMA_EMBED_MODEL', 'embeddinggemma')
EMBED_TIMEOUT = 60

SHORTLIST = 40      # what the LLM sees
FINAL = 10          # what the panel shows

# Same eight labels the index was built from; order defines the bit positions.
CATEGORIES = (
    'New Contributor Onboarding and Involvement',
    'Code Standards and Maintainability',
    'Automated Testing and Quality Assurance',
    'Community Collaboration and Engagement',
    'Documentation Practices',
    'Project Management and Governance',
    'Security Best Practices and Legal Compliance',
    'CI/CD and DevOps Automation',
)
ONBOARDING, STANDARDS, TESTING, COMMUNITY, DOCS, GOVERNANCE, SECURITY, CICD = range(8)

# Scoring weights. Measured: a multiplicative category boost (1.15 per match)
# reached 2.7x on a project matching seven categories, which swamped the cosine
# entirely -- gem5 and a Python RAG tool came out 0.67 alike because they share
# a missing-docs pattern. The boost is now additive, capped, and small enough
# that it orders near-ties rather than deciding the ranking.
W_NEED = 0.6        # what is going wrong
W_DOMAIN = 0.4      # what the project actually is
BOOST_PER_CATEGORY = 0.03
BOOST_CAP = 3


class IndexMissing(Exception):
    """No embedding index on disk -- the caller should fall back."""


_cache = {}         # (project_id, month) -> response payload
_index = None       # lazily loaded, process-lifetime


def load_index():
    """Load the index once per process, with the catalog beside it."""
    global _index
    if _index is not None:
        return _index

    if not os.path.exists(INDEX_PATH):
        raise IndexMissing(INDEX_PATH)

    data = np.load(INDEX_PATH, allow_pickle=False)
    with open(CATALOG_PATH, encoding='utf-8') as handle:
        catalog = json.load(handle)

    if len(catalog) != int(data['catalog_count']):
        # Ranking would still work but the rows would describe different
        # entries than the catalog does, which is worse than not answering.
        raise IndexMissing(
            f'index has {int(data["catalog_count"])} rows, catalog has {len(catalog)}'
            ' -- rerun scripts/build_actionable_index.py')

    _index = {
        'vectors': data['vectors'],
        'category': data['category'],
        'importance': data['importance'],
        'confidence': data['confidence'],
        'catalog': catalog,
    }
    logger.info(f'actionables index: {_index["vectors"].shape} from {INDEX_PATH}')

    return _index


def _section(digest, name):
    """A sub-object of the digest, or an empty dict if it is anything else.

    `digest.get(x) or {}` keeps a string if the browser sent one, and the next
    .get() raises. The digest is built client-side, so treat every level as
    untrusted -- the same guard build_evidence() uses.
    """
    value = digest.get(name) if isinstance(digest, dict) else None

    return value if isinstance(value, dict) else {}


def wanted_categories(digest, missing_docs, has_ci):
    """Which categories this project's own numbers argue for.

    Every rule reads a signal painPointsDigest already computes, so nothing new
    is measured here.
    """
    digest = digest if isinstance(digest, dict) else {}
    wanted = set()

    absent = {d.upper() for d in (missing_docs or [])}
    if 'CONTRIBUTING' in absent or 'CODE_OF_CONDUCT' in absent:
        wanted |= {ONBOARDING, DOCS}
    if 'SECURITY' in absent:
        wanted.add(SECURITY)
    if 'GOVERNANCE' in absent:
        wanted.add(GOVERNANCE)
    if not has_ci:
        wanted.add(CICD)

    tech = _section(digest, 'technical')
    solo = (tech.get('solo_file_types')
            if isinstance(tech.get('solo_file_types'), dict) else {})
    if _num(solo.get('total')) and _num(solo.get('count')) / _num(solo['total']) > 0.5:
        wanted.add(STANDARDS)
    if _num(tech.get('top_contributor_share')) > 0.35:
        wanted.add(GOVERNANCE)

    social = _section(digest, 'social')
    silent = (social.get('silent_developers')
              if isinstance(social.get('silent_developers'), dict) else {})
    if _num(silent.get('total')) and _num(silent.get('count')) / _num(silent['total']) > 0.5:
        wanted.add(COMMUNITY)
    if social.get('empty'):
        wanted |= {COMMUNITY, ONBOARDING}

    if _falling(_series(social, 'participants')):
        wanted |= {COMMUNITY, ONBOARDING}
    if _falling(_series(tech, 'developers')):
        wanted.add(GOVERNANCE)

    if _falling(_section(digest, 'forecast').get('series')):
        wanted.add(GOVERNANCE)

    # A project with no obvious weakness still needs a shortlist; testing and
    # standards are the safe general-purpose pair.
    return wanted or {TESTING, STANDARDS}


def _series(section, name):
    series = section.get('series')

    return series.get(name) if isinstance(series, dict) else None


def _falling(series):
    points = [p for p in (series or []) if isinstance(p, dict)]
    if len(points) < 2:
        return False

    return _num(points[-1].get('value')) < _num(points[0].get('value'))


def embed(texts):
    """Both query vectors in one call -- the endpoint accepts a list."""
    try:
        with gpu_slot('actionables-embed'):
            response = requests.post(
                f'{OLLAMA_URL}/api/embed',
                json={'model': EMBED_MODEL, 'input': texts},
                timeout=(CONNECT_TIMEOUT, EMBED_TIMEOUT))
        response.raise_for_status()
        vectors = response.json().get('embeddings') or []
    except (requests.RequestException, ValueError) as e:
        logger.error(f'actionables: embedding failed: {e}')
        raise RepoWiseUnavailable(str(e))

    if len(vectors) != len(texts):
        raise RepoWiseUnavailable('embedding model returned the wrong count')

    return [np.asarray(v, dtype=np.float32) for v in vectors]


def _unit(vector, width):
    # Truncate to the index's width, then normalise, so a dot product is the
    # cosine even when the model returns more dimensions than were stored.
    vector = vector[:width]

    return vector / max(float(np.linalg.norm(vector)), 1e-9)


def retrieve(need_text, domain_text, wanted, limit=SHORTLIST):
    """Rank the whole catalog against the project. One matrix multiply.

    Two queries rather than one. A single blended query let the metrics prose --
    which reads much the same for any struggling project -- drown out what the
    project actually is, so unlike repositories came back with near-identical
    lists. Scoring need and domain separately keeps both in play: an entry has
    to be about a real problem here AND belong in this technology.
    """
    index = load_index()
    width = index['vectors'].shape[1]

    need_vec, domain_vec = embed([need_text, domain_text or need_text])
    need = index['vectors'] @ _unit(need_vec, width)
    domain = index['vectors'] @ _unit(domain_vec, width)

    scores = W_NEED * need + W_DOMAIN * domain

    # Orders near-ties toward the categories this project's numbers argue for.
    mask = index['category']
    matches = np.zeros(len(scores), dtype=np.float32)
    for bit in wanted:
        matches += ((mask >> bit) & 1).astype(np.float32)
    scores += BOOST_PER_CATEGORY * np.minimum(matches, BOOST_CAP)

    # A light prior from the catalog's own fields. This used to be the entire
    # ranking, which is why every project saw the same ten.
    scores += 0.01 * index['importance'] + 0.02 * index['confidence']

    top = np.argsort(-scores)[:limit]

    return [(int(i), float(scores[i])) for i in top]


def build_profile(digest, project_name, payload, missing, documents):
    """(what is going wrong, what this project is).

    Kept apart because they are asked of the catalogue separately -- see
    retrieve(). The first is the pain-points evidence; the second is what stops
    a C++ simulator being told to adopt a probabilistic-programming fuzzer.
    """
    need = build_evidence(digest, project_name)
    if missing:
        need += '\n\nGOVERNANCE DOCUMENTS NOT PRESENT: ' + ', '.join(missing)

    meta = (digest.get('metadata') or {}) if isinstance(digest, dict) else {}
    about = [f'{project_name} is an open-source software project.']
    if meta.get('languages'):
        about.append('It is written in '
                     + ', '.join(str(x) for x in meta['languages'][:6]) + '.')
    if payload.get('description'):
        about.append(str(payload['description'])[:400])
    if documents:
        about.append(documents[:1200])

    return need, ' '.join(about)


def build_prompt(profile, candidates, catalog):
    lines = [f"[{i}] {(catalog[i].get('title') or '').strip()}" for i, _ in candidates]

    return f"""You are advising the maintainers of this open-source project.

Below is what is measured about the project, then {len(lines)} candidate
recommendations drawn from a research catalogue. Choose the ones that genuinely
fit THIS project and would help it now.

RULES
- Reply with one line per choice, in the form: [id] why it fits THIS project.
- Choose at most {FINAL}. Choose FEWER if fewer genuinely fit -- do not pad.
- Reject anything aimed at a different technology, language or research area
  than this project uses. A recommendation about a tool for a language this
  project does not use is wrong however good it sounds.

THE REASON IS THE HARD PART. It must say something about THIS project that
would be false of most others.
- START with the project's own evidence, never with the recommendation's words.
  If your sentence begins by repeating the title, delete it and start again.
- Every reason must contain a specific figure from the evidence, or a concrete
  fact about what this project is -- what it builds, what it is written in and
  what that implies. Never invent either.
- "because the project is written in Python" is not a reason; every Python
  project would match it. "the Cython extensions mean contributors need a build
  toolchain before their first change" is a reason.
- One sentence, under 25 words. No preamble, no heading, no summary, no URLs.

GOOD:  [12] Two developers make 60% of all changes, so a bus factor of two is
       the project's sharpest risk.
GOOD:  [7] A hardware simulator's regression suite runs for hours, which is why
       test selection matters more here than in a typical library.
BAD:   [12] Adopt network-based operationalizations for classifying core and
       peripheral developers, because 60% of changes come from two people.
       (restates the title)
BAD:   [7] This is relevant because the project is written in C++.
       (true of thousands of projects)

PROJECT EVIDENCE
{profile}

CANDIDATES
{chr(10).join(lines)}

Choices:"""


_CHOICE = re.compile(r'^\D{0,3}\[?(\d+)\]?[\s:.)-]+(?P<why>.+)$')


def parse_choices(text, allowed):
    """Ids the model picked, with its reason, dropping anything it invented."""
    chosen, seen = [], set()
    for line in (text or '').splitlines():
        match = _CHOICE.match(line.strip())
        if not match:
            continue

        index = int(match.group(1))
        why = match.group('why').strip(' -.')
        if index in allowed and index not in seen and why:
            seen.add(index)
            chosen.append((index, why))

    return chosen[:FINAL]


def as_payload(index_ids, catalog, reasons=None):
    out = []
    for i in index_ids:
        entry = dict(catalog[i])
        entry['catalog_index'] = i
        if reasons and i in reasons:
            entry['why'] = reasons[i]
        out.append(entry)

    return out


@actionables_bp.errorhandler(InferenceBusy)
def _handle_busy(_e):
    return jsonify({'message': 'The analysis service is busy right now.'
                              ' Please try again in a moment.'}), 503


@actionables_bp.errorhandler(RepoWiseUnavailable)
def _handle_unavailable(_e):
    return jsonify({'message': 'Actionable selection is temporarily unavailable.'}), 503


@actionables_bp.route('/api/actionables', methods=['POST'])
@cross_origin(origin='*')
def actionables():
    payload = request.get_json(silent=True) or {}
    digest = payload.get('digest')
    if not isinstance(digest, dict) or not digest:
        return jsonify({'message': 'No project data to select against yet.'}), 400

    project_name = str(payload.get('project_name') or 'this project')[:120]

    project_id = payload.get('project_id')
    if not project_id and payload.get('github_url'):
        try:
            project_id = project_id_from_github_url(payload['github_url'])
        except ValueError:
            project_id = None

    # Identity must come from the repository, never from the display name:
    # an unnamed project defaults to "this project", so two different repos
    # would have shared a cache entry.
    identity = project_id or payload.get('github_url')
    key = (f"{identity}::{digest.get('span', 'window')}::{digest.get('month')}"
           if identity else None)
    if key and not payload.get('refresh') and key in _cache:
        return jsonify(_cache[key]), 200

    try:
        index = load_index()
    except IndexMissing as e:
        logger.error(f'actionables: {e}')

        return jsonify({'message': 'The actionable index has not been built yet.'}), 503

    # The profile is the pain-points evidence plus what the project IS -- the
    # languages and description are what stop a C++ simulator being told to
    # adopt a probabilistic-programming fuzzer.
    documents, sources, missing = fetch_documents(project_id)
    need_text, domain_text = build_profile(digest, project_name, payload,
                                           missing, documents)

    # The prompt still sees one profile; only retrieval needs them apart.
    profile = need_text + '\n\n' + domain_text

    has_ci = bool(payload.get('has_ci'))
    wanted = wanted_categories(digest, missing, has_ci)
    shortlist = retrieve(need_text, domain_text, wanted)
    catalog = index['catalog']

    result = {
        'project': project_name,
        'categories': sorted(CATEGORIES[b] for b in wanted),
        'shortlist_size': len(shortlist),
        'sources': sources,
    }

    try:
        answer = _generate(build_prompt(profile, shortlist, catalog))
        chosen = parse_choices(answer, {i for i, _ in shortlist})
    except RepoWiseUnavailable:
        # Retrieval already produced a project-specific order; returning it
        # beats returning the importance sort the panel had before.
        logger.warning('actionables: rerank unavailable, returning retrieval order')
        chosen = []

    if chosen:
        result['actionables'] = as_payload([i for i, _ in chosen], catalog,
                                           dict(chosen))
        result['reranked'] = True
    else:
        result['actionables'] = as_payload([i for i, _ in shortlist[:FINAL]], catalog)
        result['reranked'] = False

    if key:
        _cache[key] = result

    return jsonify(result), 200
