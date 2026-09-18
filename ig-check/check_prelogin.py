#!/usr/bin/env python3
"""Checks the username screen's profile lookup from this Mac, the way the app
does it: every way in the playbook's `prelogin_profile` operation, with the
same addresses and headers, no Instagram login.

    python3 scripts/check_prelogin.py                     # looks up "instagram"
    python3 scripts/check_prelogin.py some.username
    python3 scripts/check_prelogin.py some.username firebase.json   # a playbook from Remote Config

The app stops at the first way that finds the profile; this tries every way,
so a broken one shows up even while a backup still works. It prints status
codes, outcomes and which fields came back, never the profile's details.
Mirrors lib/services/ig/ig_engine.dart (http strategies); keep them in step.
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent  # the playbook sits next to this copy
ALLOWED = re.compile(r'^([a-z0-9-]+\.)*instagram\.com$')


def read_path(root, spec):
    for alternative in spec.split('|'):
        current, found = root, True
        for segment in alternative.strip().split('.'):
            if not segment:
                continue
            if isinstance(current, dict) and segment in current:
                current = current[segment]
            elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
                current = current[int(segment)]
            else:
                found = False
                break
        if found and current is not None:
            return current
    return None


def count(text):
    plain = text.replace(',', '').strip()
    last = plain[-1:].lower()
    multiplier = {'k': 1000, 'm': 1000000}.get(last, 1)
    number = plain if multiplier == 1 else plain[:-1]
    try:
        return int(float(number) * multiplier)
    except ValueError:
        return 0


def transform(value, name):
    if value is None:
        return None
    if name == 'string':
        return str(value)
    if name == 'int':
        return int(value) if isinstance(value, (int, float)) else (int(value) if str(value).isdigit() else None)
    if name == 'bool':
        return value if isinstance(value, bool) else str(value) in ('true', '1')
    if name == 'count':
        return count(str(value))
    if name == 'html_unescape':
        return str(value).replace('&amp;', '&').replace('&#064;', '@')
    if name == 'before_underscore':
        return str(value).split('_')[0]
    if name == 'lowercase':
        return str(value).lower()
    return value


def field_value(field, node):
    if isinstance(field, str):
        return read_path(node, field) if isinstance(node, dict) else None
    if 'pattern' in field and 'path' not in field:
        if not isinstance(node, str):
            return None
        try:
            match = re.search(field['pattern'], node)
        except re.error:
            return None
        return transform(None if match is None else (match.group(1) if match.re.groups else match.group(0)), field.get('transform'))
    return transform(read_path(node, field['path']) if isinstance(node, dict) else None, field.get('transform'))


def fill(template, values):
    def replace(m):
        name, mode = m.group(1), m.group(2)
        if name not in values:
            return m.group(0)
        value = values[name]
        if mode == 'json':
            return json.dumps(value)
        if mode == 'raw':
            return value or ''
        return '' if value is None else urllib.parse.quote(value, safe='')
    return re.sub(r'\{([a-z_]+)(?::(raw|json_form|json))?\}', replace, template)


def attempt(strategy, username):
    url = fill(strategy['url'], {'username': username})
    host = urllib.parse.urlparse(url).hostname or ''
    if not ALLOWED.match(host):
        return 0, 'blocked', None, 'address outside Instagram'
    headers = {k: fill(v, {'username': username}) for k, v in (strategy.get('headers') or {}).items()}
    request = urllib.request.Request(url, headers=headers, method=strategy.get('method', 'GET'))
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, text = response.status, response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:  # noqa: BLE001 — any network failure is one outcome
        return 0, 'network', None, str(e)[:80]
    ok = 200 <= status < 300
    html = strategy.get('reply') == 'html'
    data = text
    if not html:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    message = f"{data.get('message', '')} {data.get('error_type', '')}".lower() if isinstance(data, dict) else ''
    if status == 429 or 'wait a few minutes' in message or 'feedback_required' in message:
        return status, 'rateLimited', None, message.strip() or text[:60]
    if status in (401, 403) or (isinstance(data, dict) and data.get('require_login') is True) or \
            any(word in message for word in ('login_required', 'checkpoint_required', 'challenge_required')):
        return status, 'auth', None, message.strip()
    if status in (strategy.get('not_found_status') or []):
        return status, 'notFound', None, ''
    if not html and data is None:
        return status, 'notJson' if ok else 'http', None, text[:60].replace('\n', ' ')
    if not ok:
        return status, 'http', None, message.strip() or text[:60].replace('\n', ' ')
    node = data if html or not strategy.get('object_path') else read_path(data, strategy['object_path'])
    if not (isinstance(node, str) if html else isinstance(node, dict)):
        return status, 'notFound' if strategy.get('missing_object_is_not_found') else 'shape', None, 'no profile in the reply'
    record = {key: field_value(field, node) for key, field in (strategy.get('fields') or {}).items()}
    missing = [key for key in strategy.get('require') or [] if record.get(key) in (None, '')]
    if missing:
        return status, 'shape', record, 'missing ' + ', '.join(missing)
    return status, 'ok', record, ''


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else 'instagram'
    source = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / 'ig-playbook.default.json'
    playbook = json.loads(source.read_text())
    op = (playbook.get('ops') or {}).get('prelogin_profile')
    if not op:
        sys.exit(f'{source.name} has no prelogin_profile operation')
    print(f"Username screen check · playbook {playbook.get('version')} · looking up {username}")
    first_found = None
    for strategy in op['strategies']:
        if strategy.get('enabled') is False:
            print(f"⚪ {strategy['id']}: switched off")
            continue
        status, outcome, record, detail = attempt(strategy, username)
        if outcome == 'ok':
            first_found = first_found or strategy['id']
            filled = [key for key, value in record.items() if value not in (None, '')]
            print(f"✅ {strategy['id']}: {status} ok — fields: {', '.join(filled)}")
        else:
            print(f"❌ {strategy['id']}: {status} {outcome}{' — ' + detail if detail else ''}")
    print()
    if first_found:
        print(f'Result: the app would show the profile (found by {first_found}).')
    else:
        print('Result: the app would say "Account not found". Every way failed.')


if __name__ == '__main__':
    main()
