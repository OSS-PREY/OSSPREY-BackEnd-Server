#!/usr/bin/env python3
"""Embed the ReACT actionable catalog once, so selection costs two calls, not 3837.

The naive way to make actionables project-specific is to ask the model "does
this fit?" for every entry. This does the 3837 comparisons as one matrix
multiply instead: the catalog is embedded here, offline; at request time only
the project is embedded, and numpy does the rest.

Re-run whenever updated_react_set2.json changes. The endpoint compares the
catalog's mtime against the one recorded here and warns when it has drifted.

    python3 scripts/build_actionable_index.py
    python3 scripts/build_actionable_index.py --catalog /path/to/set.json --dims 256
"""
import argparse
import hashlib
import json
import os
import sys
import time

import numpy as np
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Sibling checkouts, the layout AGENTS.md already requires for the scraper and
# forecaster. The catalog is served by the frontend at /updated_react_set2.json.
DEFAULT_CATALOG = os.path.normpath(os.path.join(
    REPO, '..', 'OSSPREY-FrontEnd-Server', 'public', 'updated_react_set2.json'))
DEFAULT_OUT = os.path.join(REPO, 'data', 'actionables_index.npz')

OLLAMA_URL = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11435').rstrip('/')
EMBED_MODEL = os.environ.get('OLLAMA_EMBED_MODEL', 'embeddinggemma')

# The eight base labels `category` is built from; it is a pipe-separated
# multi-label, so an entry can carry several. 211 entries are "NONE" and 27 are
# empty, which is why these drive a boost at query time and never a filter.
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


def entry_text(entry):
    """What gets embedded: the recommendation plus why it exists.

    Titles alone are too terse to separate 3837 entries -- many differ only in
    the tool they name -- so the evidence and the claimed impact go in too.
    """
    parts = [entry.get('title') or '',
             entry.get('evidence') or '',
             entry.get('positive_impact') or '']

    return ' '.join(p.strip() for p in parts if p.strip())


def category_mask(entry):
    """Eight bits, one per base category."""
    label = entry.get('category') or ''
    bits = 0
    for index, name in enumerate(CATEGORIES):
        if name in label:
            bits |= 1 << index

    return bits


def embed(texts, batch=64, retries=3):
    vectors = []
    for start in range(0, len(texts), batch):
        chunk = texts[start:start + batch]
        for attempt in range(retries):
            try:
                response = requests.post(
                    f'{OLLAMA_URL}/api/embed',
                    json={'model': EMBED_MODEL, 'input': chunk},
                    timeout=(5, 300))
                response.raise_for_status()
                got = response.json().get('embeddings') or []
                if len(got) != len(chunk):
                    raise ValueError(f'asked for {len(chunk)}, got {len(got)}')
                vectors.extend(got)
                break
            except (requests.RequestException, ValueError) as e:
                if attempt == retries - 1:
                    sys.exit(f'embedding failed at offset {start}: {e}')
                time.sleep(2 * (attempt + 1))

        done = min(start + batch, len(texts))
        print(f'\r  embedded {done}/{len(texts)}', end='', flush=True)
    print()

    return np.asarray(vectors, dtype=np.float32)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--catalog', default=DEFAULT_CATALOG)
    parser.add_argument('--out', default=DEFAULT_OUT)
    parser.add_argument('--dims', type=int, default=256,
                        help='Matryoshka truncation; 256 keeps the index at ~4MB')
    args = parser.parse_args()

    if not os.path.exists(args.catalog):
        sys.exit(f'catalog not found: {args.catalog}')

    with open(args.catalog, encoding='utf-8') as handle:
        catalog = json.load(handle)

    texts = [entry_text(e) for e in catalog]
    empty = sum(1 for t in texts if not t)
    if empty:
        print(f'note: {empty} entries have no text and will rank last')

    print(f'embedding {len(texts)} entries with {EMBED_MODEL} at {OLLAMA_URL}')
    vectors = embed(texts)

    # Matryoshka: the leading dimensions carry the most signal, so truncating
    # is a real trade rather than a lossy hack. 768 -> 256 takes the index from
    # 11.8MB to 3.9MB for a negligible ranking difference at top-40.
    if args.dims and args.dims < vectors.shape[1]:
        vectors = vectors[:, :args.dims]

    # Re-normalise after truncation so a dot product is exactly cosine.
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / np.maximum(norms, 1e-9)

    raw = open(args.catalog, 'rb').read()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    np.savez_compressed(
        args.out,
        vectors=vectors,
        category=np.array([category_mask(e) for e in catalog], dtype=np.uint8),
        importance=np.array([float(e.get('importance') or 0) for e in catalog],
                            dtype=np.float32),
        confidence=np.array([float(e.get('confidence_score') or 0) for e in catalog],
                            dtype=np.float32),
        # Identity of the source, so the endpoint can tell when it has drifted.
        catalog_sha=np.array(hashlib.sha256(raw).hexdigest()),
        catalog_count=np.array(len(catalog)),
        model=np.array(EMBED_MODEL),
        dims=np.array(vectors.shape[1]),
    )

    size = os.path.getsize(args.out) / 1e6
    print(f'wrote {args.out}  {vectors.shape[0]}x{vectors.shape[1]}  {size:.1f} MB')


if __name__ == '__main__':
    main()
