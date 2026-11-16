"""Read-only GitHub delivery observations; never launches or repairs work."""
from __future__ import annotations
import datetime
import json
import re
from pathlib import Path
from urllib.parse import quote

SHA = re.compile(r'^[0-9a-f]{40}$')
RUN = re.compile(r'^run-[A-Za-z0-9-]+$')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def publication(record, repository):
    """Only applied PR creation in a linked, matching publication child is evidence."""
    run = record.get('run') or {}
    run_id, trace = run.get('run_id'), run.get('trace_path')
    if not isinstance(run_id, str) or not RUN.fullmatch(run_id) or not isinstance(trace, str):
        return 'unavailable', None, None
    path = Path(trace)
    if path.name != run_id + '.json':
        return 'unavailable', None, None
    try:
        parent = json.loads(path.read_text(encoding='utf-8'))
        orchestration = parent['parent_orchestration']
        if parent.get('run_id') != run_id or orchestration['target_repository'].casefold() != repository.casefold():
            return 'unavailable', None, None
        numbers = set()
        heads = set()
        published_heads = set()
        waves = {w.get('id'): w for w in orchestration.get('waves', []) if isinstance(w, dict)}
        for phase in orchestration.get('phases', []):
            for check in phase.get('diff_floor_checks', []):
                wave = waves.get(check.get('wave_id')) or {}
                head = check.get('head')
                if (check.get('operation') == 'parent' and check.get('status') == 'passed'
                        and wave.get('status') == 'integrated' and phase.get('id') in wave.get('task_ids', [])
                        and isinstance(head, str) and SHA.fullmatch(head)):
                    heads.add(head)
        for phase in orchestration.get('phases', []):
            for child_id in phase.get('child_run_ids', []):
                if not isinstance(child_id, str) or not RUN.fullmatch(child_id):
                    return 'unavailable', None, None
                child = json.loads((path.parent / (child_id + '.json')).read_text(encoding='utf-8'))
                if child.get('run_id') != child_id:
                    return 'unavailable', None, None
                meta = child.get('github_integration') or {}
                if str(meta.get('target_repository', '')).casefold() != repository.casefold():
                    continue
                receipt = meta.get('publication_receipt') or {}
                receipt_head = receipt.get('commit')
                if (receipt.get('stage') == 'pr_observed' and receipt.get('parent_run_id') == run_id
                        and str(receipt.get('repository', '')).casefold() == repository.casefold()
                        and isinstance(receipt_head, str) and SHA.fullmatch(receipt_head)
                        and receipt_head in heads and type(receipt.get('pr')) is int and receipt['pr'] > 0):
                    numbers.add(receipt['pr'])
                    published_heads.add(receipt_head)
                for entry in meta.get('audit', []):
                    n = entry.get('pr')
                    if entry.get('operation') in ('create_pr', 'create_draft_pr') and entry.get('status') == 'applied' and type(n) is int and n > 0:
                        numbers.add(n)
        if len(numbers) > 1:
            return 'ambiguous', None, None
        bound = published_heads if published_heads else heads
        return ('observed', numbers.pop(), next(iter(bound)) if len(bound) == 1 else None) if numbers else ('none', None, None)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 'unavailable', None, None


def classify_checks(checks, required):
    if not isinstance(checks, list) or not isinstance(required, list) or not required:
        return 'unknown', []
    outcomes = {}
    for item in checks:
        if not isinstance(item, dict):
            return 'unknown', []
        name = item.get('name') or item.get('context')
        if not isinstance(name, str) or not name:
            return 'unknown', []
        status = str(item.get('status') or '').upper()
        conclusion = str(item.get('conclusion') or item.get('state') or '').upper()
        if status and status not in ('COMPLETED',):
            result = 'running'
        elif conclusion == 'SUCCESS':
            result = 'passed'
        elif conclusion in ('PENDING', 'EXPECTED', 'QUEUED', 'IN_PROGRESS', ''):
            result = 'running'
        else:
            result = 'failed'
        outcomes.setdefault(name, []).append(result)
    if not checks or any(name not in outcomes for name in required):
        return 'unknown', sorted(outcomes)
    flattened = [v for group in outcomes.values() for v in group]
    return ('failed' if 'failed' in flattened else 'running' if 'running' in flattened else 'passed'), sorted(outcomes)


def review_state(reviews, head):
    if not isinstance(reviews, list):
        return 'unknown'
    latest = {}
    for review in reviews:
        if not isinstance(review, dict):
            return 'unknown'
        author = review.get('user') or {}
        login = author.get('login')
        if not isinstance(login, str) or not login or author.get('type') != 'User':
            continue
        state = str(review.get('state') or '').upper()
        if state in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            latest[login.casefold()] = (state, review.get('commit_id'))
    if any(state == 'CHANGES_REQUESTED' and commit == head for state, commit in latest.values()):
        return 'changes_requested'
    if any(state == 'CHANGES_REQUESTED' and commit != head for state, commit in latest.values()):
        return 'stale'
    if any(state == 'APPROVED' and commit == head for state, commit in latest.values()):
        return 'approved'
    if any(state in ('APPROVED', 'CHANGES_REQUESTED') and commit != head for state, commit in latest.values()):
        return 'stale'
    return 'not_requested'


def observe(row, record, call, expected=None):
    """call(argv) must use target-scoped credentials and return decoded JSON."""
    stamp = now()
    repo = row['repo']
    item = dict(issue=row['number'], execution_id=row['execution_id'], execution=row['state'], repository=repo,
                run_id=(record.get('run') or {}).get('run_id'),
                publication='unavailable', pr_number=None, pr_url=None, head_sha=None, checks='unknown',
                review='unknown', delivery='needs_decision', reason='Publication evidence unavailable',
                next_action='Inspect execution and publication evidence', actor='operator', observed_at=stamp,
                first_seen=stamp, last_seen=stamp, evidence={})
    state, number, integrated_head = publication(record, repo)
    item['publication'] = state
    if state != 'observed':
        item['reason'] = {'none': 'No applied publication PR found', 'ambiguous': 'Multiple publication PRs found',
                          'unavailable': 'Publication evidence unavailable'}[state]
        return item
    item['pr_number'] = number
    item['pr_url'] = f'https://github.com/{repo}/pull/{number}'
    try:
        endpoint = f'repos/{repo}/pulls/{number}'
        pr = call(['api', endpoint])
        def valid_pr(p):
            return (isinstance(p, dict) and p.get('number') == number and
                    str((p.get('base') or {}).get('repo', {}).get('full_name', '')).casefold() == repo.casefold() and
                    str((p.get('head') or {}).get('repo', {}).get('full_name', '')).casefold() == repo.casefold() and
                    bool(SHA.fullmatch(str((p.get('head') or {}).get('sha', '')))) and
                    isinstance((p.get('base') or {}).get('ref'), str))
        if not valid_pr(pr):
            raise ValueError('PR identity unavailable')
        head = pr['head']['sha']
        base = pr['base']['ref']
        item['head_sha'] = head
        if integrated_head is None or head != integrated_head:
            item.update(checks='stale', review='stale', reason='PR head is not bound to a unique integrated commit', next_action='Inspect publication and integrated commit')
            return item
        if pr.get('merged_at'):
            item.update(delivery='merged', reason='Pull request merged', next_action='Review merged pull request', actor='human')
            return item
        if pr.get('state') == 'closed':
            item.update(delivery='closed', reason='Pull request closed', next_action='Review closed pull request', actor='human')
            return item
        if pr.get('state') != 'open':
            raise ValueError('Unknown PR state')
        # Configured expectations determine CI observation; branch protection and
        # rulesets are separate merge-rule evidence and may be inaccessible.
        required = list(expected) if isinstance(expected, list) and expected else None
        rules_status = 'unknown'
        try:
            rules = call(['api', f'repos/{repo}/branches/{quote(base, safe="")}/protection'])
            required_obj = rules.get('required_status_checks') if isinstance(rules, dict) else None
            contexts = required_obj.get('contexts') if isinstance(required_obj, dict) else None
            if isinstance(contexts, list) and all(isinstance(x, str) and x for x in contexts):
                if required is not None:
                    required = sorted(set(required + contexts))
                rules_status = 'branch_protection_observed'
            rulesets = call(['api', '--paginate', '--slurp', f'repos/{repo}/rules/branches/{quote(base, safe="")}'])
            if not isinstance(rulesets, list) or any(not isinstance(page, list) or page for page in rulesets):
                rules_status = 'unknown'
        except Exception:
            rules_status = 'unknown'
        checks = call(['pr', 'view', str(number), '--repo', repo, '--json', 'number,headRefOid,statusCheckRollup'])
        reviews = call(['api', '--paginate', '--slurp', f'{endpoint}/reviews'])
        if isinstance(reviews, list) and all(isinstance(page, list) for page in reviews):
            reviews = [r for page in reviews for r in page]
        again = call(['api', endpoint])
        if not valid_pr(again) or again['head']['sha'] != head or again['base']['ref'] != base or again.get('state') != pr.get('state') or again.get('merged_at') != pr.get('merged_at'):
            item.update(checks='stale', review='stale', reason='Pull request changed during observation', next_action='Wait for a fresh observation')
            return item
        if not isinstance(checks, dict) or checks.get('number') != number or checks.get('headRefOid') != head:
            item.update(checks='stale', review='stale', reason='Check head does not match pull request', next_action='Wait for a fresh observation')
            return item
        item['checks'], names = classify_checks(checks.get('statusCheckRollup'), required)
        item['review'] = review_state(reviews, head)
        if item['review'] == 'not_requested' and any(isinstance(r, dict) and r.get('type') == 'User' for r in pr.get('requested_reviewers', [])):
            item['review'] = 'requested'
        item['evidence'] = {'required_checks': sorted(required) if isinstance(required, list) and all(isinstance(x, str) for x in required) else [],
                            'observed_checks': names, 'base': base, 'rules': rules_status,
                            'merge_readiness': 'unknown', 'publication_head': 'not recorded'}
        if item['checks'] == 'failed' or item['review'] == 'changes_requested':
            item.update(reason='Checks failed or a human requested changes', next_action='Inspect checks and review findings')
        elif item['checks'] == 'running':
            item.update(delivery='waiting_ci', reason='Checks are running', next_action='Wait for checks', actor='github')
        elif item['checks'] == 'passed' and item['review'] == 'approved':
            item.update(reason='Current human approval observed; merge eligibility is not established',
                        next_action='Check required reviews and branch rules in GitHub before deciding to merge', actor='human')
        elif item['checks'] == 'passed' and item['review'] != 'unknown':
            item.update(delivery='needs_human_review', reason='Expected checks passed; human review needed; merge rules may be incomplete', next_action='Review the pull request', actor='human')
        else:
            item.update(reason='Required checks or review evidence is incomplete', next_action='Inspect branch rules and pull request')
    except Exception:
        # Do not expose provider errors, URLs with credentials, or raw responses.
        item.update(checks='unavailable', review='unknown', reason='GitHub observation unavailable', next_action='Restore target access and retry')
    return item
