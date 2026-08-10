"""The ``getting-project-pain-points`` pipeline.

Takes the digest OSSPREY computes from its own pipeline output -- sustainability
forecast, technical and social networks, repository metadata -- pairs it with the
project's governance documents retrieved from RepoWise, and asks the LLM what is
going wrong right now.

Why the documents come from RepoWise but the prompt does not go through its
``/api/query``: that endpoint classifies intent before it retrieves anything, and
an analysis prompt carrying metrics comes back ``OUT_OF_SCOPE`` at confidence
1.0 -- measured, not assumed. Its document prompt then forbids inference
outright ("DO NOT make logical inferences beyond what is explicitly stated"),
which is precisely the job here. So RepoWise does what it is good at, serving the
right document chunks out of its vector store, and this module owns the prompt.

RepoWise holds no socio-technical data at all -- no networks, no forecast, no
OSSPREY metrics -- so every number in the prompt has to be supplied explicitly.
Only the markdown context is already on its side.
"""
import logging
import os
import re

import requests
from flask import Blueprint, jsonify, request
from flask_cors import cross_origin

from app.repowise import RepoWiseUnavailable, project_id_from_github_url

logger = logging.getLogger(__name__)

pain_points_bp = Blueprint('pain_points', __name__)

REPOWISE_URL = os.environ.get('REPOWISE_BASE_URL', 'http://localhost:8000').rstrip('/')
OLLAMA_URL = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434').rstrip('/')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'mistral:latest')

# Reasoning models (gemma4, qwen3) think by default, and Ollama routes that into
# a separate field -- gemma4:12b spent all 700 tokens there and returned an empty
# response. Set OLLAMA_THINK=false for those; leave it unset for mistral, whose
# older Ollama build has no such parameter.
OLLAMA_THINK = os.environ.get('OLLAMA_THINK')

CONNECT_TIMEOUT = 5
SEARCH_TIMEOUT = 30
GENERATE_TIMEOUT = 300      # Mistral on CPU takes minutes for a 700-token answer.

MAX_DOC_CHARS = 5000
MAX_EVIDENCE_CHARS = 6000
MAX_BULLETS = 8

# Three angles rather than one: a single embedding query over a long analysis
# prompt retrieves badly, and these are the document areas a pain point tends to
# live in.
DOC_QUERIES = (
    'contribution process pull request review merge requirements',
    'maintainers governance decision making project leadership',
    'code of conduct security policy reporting problems',
)

# Files whose absence is itself a finding, so the prompt can be told what is
# missing rather than only what is present.
EXPECTED_DOCS = (
    'README', 'CONTRIBUTING', 'CODE_OF_CONDUCT', 'SECURITY', 'GOVERNANCE', 'LICENSE',
)

# Bundled third-party code. gem5 vendors pybind11, and its SECURITY.md was being
# read as gem5's own security policy -- a finding about someone else's project.
VENDORED = ('ext/', 'vendor/', 'third_party/', 'thirdparty/', 'node_modules/', 'deps/')

# One broad sweep to learn which documents the project actually has. Presence
# cannot be inferred from the targeted queries: they retrieve what matches, so
# every unmatched file looked missing, and the analysis reported gem5's README
# and LICENSE as absent when both are indexed.
INVENTORY_QUERY = 'project'
INVENTORY_RESULTS = 40


def _is_vendored(path):
    return any(marker in (path or '').lower() for marker in VENDORED)


def _search(project_id, query, n_results):
    try:
        response = requests.post(
            f'{REPOWISE_URL}/api/search',
            json={'project_id': project_id, 'query': query, 'n_results': n_results},
            timeout=(CONNECT_TIMEOUT, SEARCH_TIMEOUT))

        return response.json().get('results', []) if response.ok else []
    except (requests.RequestException, ValueError) as e:
        logger.warning(f'Pain points: RepoWise search failed for {project_id}: {e}')

        return []


def missing_documents(project_id):
    """Which of the expected governance files this project does not have."""
    paths = {
        (r.get('file_path') or '').rsplit('/', 1)[-1].upper()
        for r in _search(project_id, INVENTORY_QUERY, INVENTORY_RESULTS)
        if not _is_vendored(r.get('file_path'))
    }
    if not paths:
        return []      # Retrieval failed; absence of evidence is not a finding.

    return [doc for doc in EXPECTED_DOCS if not any(doc in name for name in paths)]


# "- x", "* x", "\u2022 x", "1. x", "1) x" -- models mix all five in one answer.
_BULLET = re.compile(r'^(?:[-*\u2022]|\d+[.)])\s+(?P<body>.*)$')


def _num(value, default=0):
    """Digest values arrive from the browser; treat every one as untrusted."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default

    return default if result != result else result   # NaN


def _pct(value):
    return f'{round(_num(value) * 100)}%'


def _trend_line(label, series):
    """"devs: m9=31, m10=24, m11=19, m12=14 (down 55%)"."""
    points = [p for p in (series or []) if isinstance(p, dict)][-6:]
    if not points:
        return None

    rendered = ', '.join(
        f"m{p.get('month')}={round(_num(p.get('value')), 3):g}" for p in points)

    first, last = _num(points[0].get('value')), _num(points[-1].get('value'))
    if first > 0 and len(points) > 1:
        change = (last - first) / first
        direction = 'down' if change < 0 else 'up'
        rendered += f' ({direction} {abs(round(change * 100))}% across the window)'

    return f'{label}: {rendered}'


def build_evidence(digest, project_name):
    """Render the digest as the plain-text block the LLM reasons over.

    Deliberately flat text rather than JSON: the model quotes numbers back far
    more reliably from prose than from nested objects.
    """
    digest = digest if isinstance(digest, dict) else {}
    lines = [f'PROJECT: {project_name}']

    meta = digest.get('metadata') or {}
    if isinstance(meta, dict) and meta:
        facts = []
        for key in ('stars', 'forks', 'watchers'):
            if meta.get(key) is not None:
                facts.append(f'{key}={int(_num(meta[key]))}')
        if meta.get('languages'):
            facts.append('languages=' + ', '.join(str(x) for x in meta['languages'][:5]))
        if meta.get('updated_at'):
            facts.append(f"last updated={meta['updated_at']}")
        if facts:
            lines.append('REPOSITORY: ' + '; '.join(facts))

    forecast = digest.get('forecast') or {}
    if isinstance(forecast, dict):
        block = ['', 'SUSTAINABILITY FORECAST (probability the project stays active, 0-1)']
        line = _trend_line('  monthly', forecast.get('series'))
        if line:
            block.append(line)
        if forecast.get('latest') is not None:
            block.append(f"  latest={round(_num(forecast['latest']), 3):g}")
        if len(block) > 2:
            lines.extend(block)

    tech = digest.get('technical') or {}
    if isinstance(tech, dict) and tech:
        block = ['', 'TECHNICAL NETWORK (who changes which files)']
        for label, key in (('  active developers', 'developers'),
                           ('  files touched', 'files'),
                           ('  file changes', 'changes')):
            line = _trend_line(label, (tech.get('series') or {}).get(key))
            if line:
                block.append(line)
        if tech.get('top_contributor_share') is not None:
            block.append(
                f"  busiest developer accounts for {_pct(tech['top_contributor_share'])}"
                ' of file changes this month')
        if tech.get('top_two_share') is not None:
            block.append(
                f"  top two developers together account for {_pct(tech['top_two_share'])}")
        solo = tech.get('solo_files') or {}
        if solo.get('total'):
            block.append(
                f"  {int(_num(solo.get('count')))} of {int(_num(solo['total']))} files"
                ' this month were touched by only one developer')
        if len(block) > 1:
            lines.extend(block)

    social = digest.get('social') or {}
    if isinstance(social, dict) and social:
        block = ['', 'SOCIAL NETWORK (who talks to whom on issues)']
        for label, key in (('  participants', 'participants'),
                           ('  messages', 'messages')):
            line = _trend_line(label, (social.get('series') or {}).get(key))
            if line:
                block.append(line)
        if social.get('top_responder_share') is not None:
            block.append(
                f"  busiest participant accounts for {_pct(social['top_responder_share'])}"
                ' of all discussion this month')
        silent = social.get('silent_developers') or {}
        if silent.get('total'):
            block.append(
                f"  silent committers: {int(_num(silent.get('count')))}"
                f" of the {int(_num(silent['total']))} developers who committed this"
                ' month wrote nothing in any discussion')
        if social.get('empty'):
            block.append('  no discussion activity was recorded this month at all')
        if len(block) > 1:
            lines.extend(block)

    return '\n'.join(lines)[:MAX_EVIDENCE_CHARS]


def fetch_documents(project_id):
    """Governance chunks from RepoWise's vector store, plus which docs exist.

    A RepoWise that is down or has never seen this project is not fatal: the
    metrics alone still support pain points, so this degrades to no context
    rather than failing the request.
    """
    if not project_id:
        return '', [], []

    seen, chunks, sources = set(), [], []
    total = 0

    for query in DOC_QUERIES:
        for result in _search(project_id, query, 3):
            path = result.get('file_path') or 'unknown'
            content = (result.get('content') or '').strip()
            key = (path, content[:80])
            if not content or key in seen or _is_vendored(path):
                continue
            if total + len(content) > MAX_DOC_CHARS:
                continue

            seen.add(key)
            total += len(content)
            chunks.append(f'[{path}]\n{content}')
            sources.append({'file_path': path, 'file_type': result.get('file_type', '')})

    return '\n\n'.join(chunks), sources, missing_documents(project_id)


def build_prompt(evidence, documents, missing_docs, project_name):
    """The analysis prompt.

    The rules are shaped by what the model actually did wrong when this was
    probed: it invented GitHub URLs for the files it cited, and it drifted from
    naming problems into proposing fixes -- which is the actionables panel's job,
    not this one's.
    """
    doc_section = (
        f'GOVERNANCE DOCUMENTS FOR {project_name}:\n{documents}'
        if documents else
        'GOVERNANCE DOCUMENTS: none were retrieved for this project.'
    )
    if missing_docs:
        doc_section += ('\n\nNOT PRESENT in the indexed documents: '
                        + ', '.join(missing_docs))

    return f"""You are an open-source project health analyst reviewing {project_name}.

Identify the project's PAIN POINTS: the things going wrong RIGHT NOW that need
immediate attention.

A pain point is a PROBLEM, not a solution. Do NOT recommend actions, do NOT
suggest fixes, do NOT write "should" or "consider" or "the team could". Another
part of this dashboard already handles recommendations. Your job is only to name
what is wrong.

RULES
- Output markdown bullets and nothing else. No preamble, no heading, no closing
  summary.
- At most {MAX_BULLETS} bullets. Fewer is better if the evidence is thin.
- One problem per bullet. Start with the problem in plain words, then give the
  number that proves it in parentheses.
- Every bullet must end with the specific number or filename that proves it, in
  parentheses. Cite the evidence itself ("active developers 31 -> 14"), never
  the name of a section of this prompt.
- Every number you write must appear verbatim in the evidence below. Never
  estimate, round differently, or invent a figure.
- Do NOT calculate anything. Percentages of change are already given in the
  evidence; copy them exactly or leave them out. A percentage you work out
  yourself will be wrong.
- Refer to documents by filename only (CONTRIBUTING.md). Never write a URL, and
  never cite a file that is not listed below.
- The documents below are EXCERPTS, not whole files. Never say a document
  "does not mention" or "lacks" or "fails to specify" something -- you cannot
  see what was not retrieved. Only the NOT PRESENT list proves absence.
- Report a document problem only when the document is absent, or when what it
  says is itself the problem. A normal, working practice is not a pain point:
  requiring review before merge, or having a release schedule, is how healthy
  projects operate.
- State only what the evidence shows. No "could lead to", "potentially",
  "may cause" -- if the harm is speculation, the bullet does not belong.
- Order the bullets most urgent first.
- If a signal looks healthy, say nothing about it. Silence is the correct output
  for a project with no problems.

EVIDENCE FROM THE OSSPREY PIPELINE
{evidence}

{doc_section}

Pain points:"""


def _generate(prompt):
    payload = {
        'model': OLLAMA_MODEL,
        'prompt': prompt,
        'stream': False,
        'options': {'temperature': 0.2, 'num_predict': 700, 'top_p': 0.9},
    }
    if OLLAMA_THINK is not None:
        payload['think'] = OLLAMA_THINK.strip().lower() == 'true'

    try:
        response = requests.post(f'{OLLAMA_URL}/api/generate', json=payload,
                                 timeout=(CONNECT_TIMEOUT, GENERATE_TIMEOUT))
    except requests.RequestException as e:
        logger.error(f'Pain points: LLM call failed: {e}')
        raise RepoWiseUnavailable(str(e))

    if not response.ok:
        logger.error(f'Pain points: LLM returned {response.status_code}: {response.text[:300]}')
        raise RepoWiseUnavailable(f'LLM returned {response.status_code}')

    try:
        return (response.json().get('response') or '').strip()
    except ValueError:
        raise RepoWiseUnavailable('LLM returned a non-JSON body')


def to_bullets(text):
    """Keep the bullet lines, drop whatever prose the model wrapped them in.

    Models reliably ignore "no preamble" some of the time, and a stray "Here are
    the pain points:" line renders as a bullet-less paragraph in the panel.
    """
    bullets = []
    for line in (text or '').splitlines():
        match = _BULLET.match(line.strip())
        if match and match.group('body').strip():
            body = re.sub(r'[*_' + chr(96) + ']{1,2}', '', match.group('body')).strip()
            if body:
                bullets.append('- ' + body)

    return bullets[:MAX_BULLETS]


@pain_points_bp.errorhandler(RepoWiseUnavailable)
def _handle_unavailable(_e):
    return jsonify({'message': 'Pain point analysis is temporarily unavailable.'}), 503


@pain_points_bp.route('/api/pain-points', methods=['POST'])
@cross_origin(origin='*')
def pain_points():
    payload = request.get_json(silent=True) or {}
    digest = payload.get('digest')
    if not isinstance(digest, dict) or not digest:
        return jsonify({'message': 'No project data to analyse yet.'}), 400

    project_name = str(payload.get('project_name') or 'this project')[:120]

    # Documents are optional: Apache and Eclipse projects carry no GitHub URL,
    # and their pain points come from the networks alone.
    project_id = payload.get('project_id')
    if not project_id and payload.get('github_url'):
        try:
            project_id = project_id_from_github_url(payload['github_url'])
        except ValueError:
            project_id = None

    evidence = build_evidence(digest, project_name)
    documents, sources, missing = fetch_documents(project_id)
    answer = _generate(build_prompt(evidence, documents, missing, project_name))
    bullets = to_bullets(answer)

    if not bullets:
        logger.info(f'Pain points: no bullets parsed for {project_name}')
        return jsonify({
            'pain_points': [],
            'message': 'No pain points stood out from the available signals.',
            'sources': sources,
        }), 200

    return jsonify({
        'pain_points': bullets,
        'sources': sources,
        'documents_used': bool(documents),
    }), 200
