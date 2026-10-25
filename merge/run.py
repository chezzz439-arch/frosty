"""PR Guardian decision executor. Run: python merge/run.py <pr_number>.

Environment: GITHUB_REPOSITORY, GITHUB_TOKEN, SNOWFLAKE_ACCOUNT,
SNOWFLAKE_USER, SNOWFLAKE_PASSWORD (or SNOWFLAKE_TOKEN), SNOWFLAKE_DATABASE,
SNOWFLAKE_SCHEMA, SNOWFLAKE_WAREHOUSE, SLACK_WEBHOOK_URL.
Optional: GUARDIAN_DRY_RUN=1, GUARDIAN_MIN_CONFIDENCE=0.85.
"""
import argparse
import json
import os
import sys
import time
from urllib.parse import quote

import requests
import snowflake.connector


def env(name, default=None):
    value = os.getenv(name, default)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def github(method, path, **kwargs):
    url = f"https://api.github.com/repos/{env('GITHUB_REPOSITORY')}{path}"
    response = requests.request(method, url, timeout=30, headers={
        'Authorization': f"Bearer {env('GITHUB_TOKEN')}",
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else {}


def private_key_der(text):
    """PEM text -> DER bytes, which is the only form the connector accepts.

    Passing the PEM string straight to `private_key` fails with
    "251008: Failed to decode private key: Incorrect padding" -- the connector
    wants base64-encoded DER, not PEM. Verified against Snowflake live; the
    same conversion is in engine/db.py.
    """
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(
        text.replace('\\n', '\n').strip().encode(), password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


def db_connect():
    # Database/schema/warehouse are the four-table contract, not secrets, so
    # they default here instead of being required env vars -- the workflow
    # does not set them and a missing one aborted the merge step.
    args = dict(account=env('SNOWFLAKE_ACCOUNT'), user=env('SNOWFLAKE_USER'),
                database=os.getenv('SNOWFLAKE_DATABASE') or 'PR_GUARDIAN',
                schema=os.getenv('SNOWFLAKE_SCHEMA') or 'CORE',
                warehouse=os.getenv('SNOWFLAKE_WAREHOUSE') or 'PR_GUARDIAN_WH')
    if os.getenv('SNOWFLAKE_ROLE'):
        args['role'] = os.environ['SNOWFLAKE_ROLE']
    # Key-pair first: that is how this account actually authenticates. Password
    # auth is left as a fallback but is not what the demo uses.
    if os.getenv('SNOWFLAKE_PRIVATE_KEY'):
        args.update(authenticator='SNOWFLAKE_JWT',
                    private_key=private_key_der(os.environ['SNOWFLAKE_PRIVATE_KEY']))
    elif os.getenv('SNOWFLAKE_PRIVATE_KEY_FILE'):
        with open(os.environ['SNOWFLAKE_PRIVATE_KEY_FILE']) as handle:
            args.update(authenticator='SNOWFLAKE_JWT',
                        private_key=private_key_der(handle.read()))
    elif os.getenv('SNOWFLAKE_TOKEN'):
        args.update(authenticator='oauth', token=os.environ['SNOWFLAKE_TOKEN'])
    else:
        args['password'] = env('SNOWFLAKE_PASSWORD')
    return snowflake.connector.connect(**args)


def event(db, pr, step, status, detail):
    with db.cursor() as cur:
        cur.execute('INSERT INTO PR_EVENTS (pr_number, step, status, detail, ts) VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP())',
                    (pr, step, status, str(detail)[:4000]))


def verdict(db, pr):
    with db.cursor() as cur:
        cur.execute('SELECT decision, confidence, reason, fixed_code, tests_passed, attempts FROM VERDICTS WHERE pr_number=%s ORDER BY created_at DESC LIMIT 1', (pr,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f'No verdict found for PR #{pr}')
    return dict(zip(('decision','confidence','reason','fixed_code','tests_passed','attempts'), row))


def decode_files(raw):
    """Expected contract: JSON {\"files\":[{\"path\":\"src/a.py\",\"content\":\"...\"}]}.
    ASSUMPTION: Kaushik serializes fixed_code this way; agree on format before integration.
    """
    obj = json.loads(raw) if isinstance(raw, str) else raw
    files = obj['files'] if isinstance(obj, dict) else obj
    if not isinstance(files, list) or not files:
        raise ValueError('fixed_code must contain nonempty files list')
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not isinstance(item.get('content'), str):
            raise ValueError('Each file needs path and content strings')
        path = item['path']
        if path.startswith('/') or '..' in path.split('/') or '\\' in path or path.startswith('.github/workflows/'):
            raise ValueError(f'Unsafe file path: {path}')
    return files


def merge_pr(pr, attempts=10, pause=3):
    """Squash-merge, waiting for GitHub to work out whether it can.

    Right after push_fix lands a commit, `mergeable` is null while GitHub
    recomputes it, and the merge endpoint answers 405 Method Not Allowed. That
    is "ask again in a moment", not "refused" -- it failed the first live run
    this way. Anything other than 405 still raises straight away, so a genuine
    refusal (conflict, closed PR, protected branch) is not retried into a loop.
    """
    for attempt in range(attempts):
        info = github('GET', f'/pulls/{pr}')
        if info['mergeable'] is None:
            time.sleep(pause)
            continue
        if not info['mergeable']:
            raise RuntimeError(f"PR #{pr} is not mergeable: {info.get('mergeable_state')}")
        try:
            return github('PUT', f'/pulls/{pr}/merge', json={'merge_method': 'squash'})
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 405 or attempt == attempts - 1:
                raise
            time.sleep(pause)
    raise RuntimeError(f'PR #{pr}: GitHub never reported a mergeable state')


def push_fix(pr, files):
    """Update the PR's own branch using the GitHub Git Data API; reject forks."""
    info = github('GET', f'/pulls/{pr}')
    head = info['head']
    if head['repo']['full_name'] != env('GITHUB_REPOSITORY'):
        raise RuntimeError('Fork PRs require human review; cannot safely push')
    if info['state'] != 'open' or info.get('draft'):
        raise RuntimeError('PR must be open and not draft')
    branch = head['ref']
    original_sha = head['sha']
    # Check head again immediately before writing to reduce stale-verdict risk.
    ref = github('GET', f"/git/ref/heads/{quote(branch, safe='')}")
    if ref['object']['sha'] != original_sha:
        raise RuntimeError('PR head changed since verdict; refusing stale fix')
    commit = github('GET', f'/git/commits/{original_sha}')
    tree_entries = []
    for item in files:
        blob = github('POST', '/git/blobs', json={'content':item['content'], 'encoding':'utf-8'})
        tree_entries.append({'path':item['path'], 'mode':'100644', 'type':'blob', 'sha':blob['sha']})
    tree = github('POST', '/git/trees', json={'base_tree':commit['tree']['sha'], 'tree':tree_entries})
    new_commit = github('POST', '/git/commits', json={
        'message': f'PR Guardian: apply tested fix for PR #{pr}',
        'tree': tree['sha'], 'parents':[original_sha]})
    github('PATCH', f"/git/refs/heads/{quote(branch, safe='')}", json={'sha':new_commit['sha'], 'force':False})
    return new_commit['sha']


def add_label(pr):
    try:
        github('POST', '/labels', json={'name':'needs-human','color':'d73a4a','description':'PR Guardian requests human review'})
    except requests.HTTPError as exc:
        if exc.response.status_code != 422:
            raise
    github('POST', f'/issues/{pr}/labels', json={'labels':['needs-human']})


def receipt_comment(pr, decision, v, merged, changed):
    body = (f'## PR Guardian receipt\n\n'
            f'- **Decision:** `{decision}`\n'
            f'- **Reason:** {v["reason"]}\n'
            f'- **Confidence:** {v["confidence"]}\n'
            f'- **Fix attempts:** {v["attempts"]}\n'
            f'- **Tests passed:** {v["tests_passed"]}\n'
            f'- **Merged:** {merged}\n'
            f'- **Changes:** {changed}\n\n'
            'Generated by PR Guardian.')
    return github('POST', f'/issues/{pr}/comments', json={'body':body})['html_url']


def slack(pr, decision, reason, merged, url):
    webhook = os.getenv('SLACK_WEBHOOK_URL')
    if not webhook:
        raise RuntimeError('Missing SLACK_WEBHOOK_URL')
    response = requests.post(webhook, json={'text':f'PR Guardian · PR #{pr} · {decision.upper()} · merged={merged}\n{reason}\n{url}'}, timeout=15)
    response.raise_for_status()


def process(pr):
    with db_connect() as db:
        v = verdict(db, pr)
        decision = str(v['decision']).strip().lower()
        if decision not in ('fix','allow','escalate'):
            raise RuntimeError(f'Unknown decision: {decision}')
        event(db, pr, 'verdict received', 'ok', decision)
        # ASSUMPTION: confidence threshold 0.85; coordinate with Kaushik.
        threshold = float(os.getenv('GUARDIAN_MIN_CONFIDENCE', '0.85'))
        if decision == 'fix' and (v['tests_passed'] is not True or v['confidence'] is None or float(v['confidence']) < threshold):
            decision = 'escalate'
            v['reason'] = f"Fix blocked by merge safety gate. Original reason: {v['reason']}"
        if os.getenv('GUARDIAN_DRY_RUN') == '1':
            print(json.dumps({'pr':pr,'effective_decision':decision,'verdict':v}, default=str, indent=2))
            return
        merged = False
        changed = 'No files changed'
        if decision == 'fix':
            files = decode_files(v['fixed_code'])
            # WARNING: engine tests must be tied to the exact PR head and fixed content.
            # An updated branch requires GitHub required checks to run on the new commit.
            push_fix(pr, files)
            changed = ', '.join(item['path'] for item in files)
            event(db, pr, 'fix pushed', 'ok', changed)
            # ASSUMPTION: required branch checks enforce tests for this new commit.
            # GitHub merge API may refuse while checks are pending; fail closed.
            merge_pr(pr)
            merged = True
            event(db, pr, 'merged', 'ok', 'Tested fix merged')
        elif decision == 'allow':
            merge_pr(pr)
            merged = True
            event(db, pr, 'merged', 'ok', 'Allowed as-is')
        else:
            add_label(pr)
            event(db, pr, 'needs-human label', 'ok', 'Escalated without merge')
        url = f"https://github.com/{env('GITHUB_REPOSITORY')}/pull/{pr}"
        comment_url = receipt_comment(pr, decision, v, merged, changed)
        event(db, pr, 'receipt posted', 'ok', comment_url)
        with db.cursor() as cur:
            cur.execute('INSERT INTO RECEIPTS (pr_number, decision, reason, merged, receipt_url, created_at) VALUES (%s,%s,%s,%s,%s,CURRENT_TIMESTAMP())',
                        (pr,decision,v['reason'],merged,comment_url))
        event(db, pr, 'receipt stored', 'ok', comment_url)
        slack(pr, decision, v['reason'], merged, url)
        event(db, pr, 'Slack sent', 'ok', url)
        print(json.dumps({'pr':pr,'decision':decision,'merged':merged,'receipt_url':comment_url}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('pr_number', type=int)
    args = parser.parse_args()
    try:
        process(args.pr_number)
    except Exception as exc:
        print(f'PR Guardian merge failed: {exc}', file=sys.stderr)
        sys.exit(1)
