"""Durable, sanitized post-execution delivery observations."""
from __future__ import annotations
import json
import re
import sqlite3
from pathlib import Path
import jsonschema

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / 'schemas/dispatcher-delivery.schema.json').read_text())


class DeliveryStoreError(ValueError):
    """Observations exist but cannot be read or validated; never treat as empty."""

FIELDS = ('issue', 'execution_id', 'run_id', 'execution', 'repository', 'publication', 'pr_number', 'pr_url',
          'head_sha', 'checks', 'review', 'delivery', 'reason', 'next_action', 'actor', 'observed_at',
          'first_seen', 'last_seen', 'evidence')


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS deliveries (issue INTEGER PRIMARY KEY, execution_id TEXT NOT NULL, projection TEXT NOT NULL)')
    db.commit()


def save(db, item):
    safe = {key: item.get(key) for key in FIELDS}
    jsonschema.validate(safe, SCHEMA)
    db.execute('INSERT INTO deliveries VALUES (?,?,?) ON CONFLICT(issue) DO UPDATE SET execution_id=excluded.execution_id, projection=excluded.projection',
               (safe['issue'], safe['execution_id'], json.dumps(safe, sort_keys=True)))
    db.commit()


def list_deliveries(state_path):
    """Read only the public projection; absent/legacy databases return no deliveries."""
    path = Path(state_path)
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deliveries'").fetchone() is None:
                return []  # A genuine pre-migration database, not a corrupt one.
            rows = db.execute('SELECT projection FROM deliveries ORDER BY issue').fetchall()
        result = []
        for row in rows:
            if len(row[0]) > 32768:
                raise DeliveryStoreError('Delivery observation exceeds size limit')
            item = json.loads(row[0])
            jsonschema.validate(item, SCHEMA)
            if (isinstance(item, dict) and type(item.get('issue')) is int and item['issue'] > 0
                    and isinstance(item.get('repository'), str)
                    and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', item['repository'])
                    and isinstance(item.get('execution_id'), str)
                    and re.fullmatch(r'issue-[A-Za-z0-9-]{1,80}', item['execution_id'])):
                safe = {key: item.get(key) for key in FIELDS}
                number = safe['pr_number']
                safe['pr_url'] = (f"https://github.com/{safe['repository']}/pull/{number}"
                                  if type(number) is int and number > 0 and safe['publication'] == 'observed' else None)
                if safe['head_sha'] is not None and not re.fullmatch(r'[0-9a-f]{40}', safe['head_sha']):
                    raise DeliveryStoreError('Delivery head is invalid')
                for key in ('reason', 'next_action', 'actor', 'observed_at', 'first_seen', 'last_seen'):
                    value = safe.get(key)
                    safe[key] = value[:300] if isinstance(value, str) else None
                evidence = safe.get('evidence')
                safe['evidence'] = {key: ([x[:200] for x in value[:100] if isinstance(x, str)] if isinstance(value, list) else value[:200] if isinstance(value, str) else None)
                                    for key, value in evidence.items() if key in ('required_checks', 'observed_checks', 'base', 'rules', 'merge_readiness', 'publication_head')} if isinstance(evidence, dict) else {}
                safe.update(queue_issue=safe['issue'], execution_state=safe['execution'], delivery_state=safe['delivery'], checks_state=safe['checks'], review_state=safe['review'])
                result.append(safe)
            else:
                raise DeliveryStoreError('Delivery identity is invalid')
        return result
    except (sqlite3.Error, ValueError, OSError, TypeError, jsonschema.ValidationError) as exc:
        raise DeliveryStoreError('Delivery observations unavailable') from exc
