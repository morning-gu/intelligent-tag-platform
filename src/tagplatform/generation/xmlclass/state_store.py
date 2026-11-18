"""Build the shared vocabulary from discoveries in retained benchmark reports."""

import json
import logging
import sqlite3
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path

from . import XMLCLASS_VERSION
from .pipeline import Candidate, DiscoveryState, Label, normalize

logger = logging.getLogger(__name__)


@lru_cache(maxsize=128)
def _report_discovery(path, modified_ns, size):
    report = json.loads(path.read_text(encoding='utf-8'))
    snapshot = report.get('discovery_state')
    if report.get('algorithm') != XMLCLASS_VERSION or not snapshot:
        return [], set(), []

    shared = {row['key']: Label(**row) for row in snapshot['labels'] if row.get('tag_id') is None}
    labels = {row['label']['key']: Label(**row['label']) for row in snapshot['candidates']}
    labels.update(shared)
    items = [item for item in report['items'] if item.get('stage') in ('evaluating', 'completed')]
    merged_into = {}
    for item in items:
        for admission in item.get('audit', {}).get('admissions', []):
            key = admission['label']['key']
            if admission.get('matched_label'):
                merged_into[key] = admission['matched_label']

    findings = []
    for item in items:
        for keyword in item.get('audit', {}).get('keywords', []):
            key = keyword.get('candidate') or keyword.get('matched_label')
            key = merged_into.get(key, key)
            if key in labels:
                occurrence = {field: keyword[field] for field in ('asset_id', 'scene_id', 'raw_term', 'quote')}
                findings.append((labels[key], occurrence))
    return findings, set(shared), [item['asset_id'] for item in items]


def _discovery_from_reports(root, library, min_documents):
    state = DiscoveryState.from_library(library)
    catalog_names = {normalize(name) for label in state.labels for name in (label.name, *label.aliases)}
    candidates = {}
    admitted = set()
    assets = {}
    for path in sorted(root.glob('bench_*.json')):
        try:
            stat = path.stat()
            findings, shared_keys, processed = _report_discovery(path, stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            logger.warning('无法从基准报告恢复发现状态 %s', path, exc_info=True)
            continue
        admitted.update(shared_keys)
        assets.update(dict.fromkeys(processed))
        for label, occurrence in findings:
            if not library.allows_new_tags(label.dir_id) or normalize(label.name) in catalog_names:
                continue
            candidate = candidates.setdefault(label.key, Candidate(label))
            if occurrence not in candidate.occurrences:
                candidate.occurrences.append(occurrence)

    for key, candidate in candidates.items():
        if key in admitted and candidate.support >= min_documents:
            state.labels.append(candidate.label)
        else:
            state.candidates.append(candidate)
    state.processed_assets = list(assets)
    return state


@contextmanager
def discovery_transaction(root, library, min_documents):
    root = Path(root)
    # Serialize the read, generation and report publication for each film.
    # Only retained reports contribute evidence, including across restarts.
    with closing(sqlite3.connect(root / 'xmlclass-state.sqlite3', timeout=900)) as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('DROP TABLE IF EXISTS discovery')  # Remove the legacy aggregate snapshot.
        yield _discovery_from_reports(root, library, min_documents)
