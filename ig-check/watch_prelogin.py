#!/usr/bin/env python3
"""Watch the username screen's profile lookup and shout only when it is truly down.

Runs every way the app has of looking up a profile without logging in — the
screen every new user reaches before they pay. If all of them fail, the funnel
earns nothing, so that is worth waking someone for.

    python3 ig-check/watch_prelogin.py            # check, alert if all ways fail
    python3 ig-check/watch_prelogin.py --dry-run  # check and print, never send

Why "all": a single way gets rate-limited in normal use (Instagram answers 401
"please wait a few minutes"), and the app just moves to the next one. Alerting
per way would cry wolf until nobody reads the alerts.

Sends to Telegram when IG_ALERT_TELEGRAM_BOT_TOKEN and IG_ALERT_TELEGRAM_CHAT_ID
are set. Prints no usernames, ids or tokens.
"""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACCOUNT = os.environ.get('IG_CHECK_ACCOUNT', 'instagram')  # a public account, never the user's


def run_check() -> tuple[bool, str]:
    """(everything failed?, the report to show)"""
    result = subprocess.run(
        [sys.executable, str(HERE / 'check_prelogin.py'), ACCOUNT],
        capture_output=True, text=True, timeout=180)
    report = (result.stdout or '') + (result.stderr or '')
    ways = [l for l in report.splitlines() if l.startswith(('✅', '❌'))]
    all_failed = bool(ways) and all(l.startswith('❌') for l in ways)
    return all_failed, report.strip()


def tell_telegram(text: str) -> bool:
    token = os.environ.get('IG_ALERT_TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.environ.get('IG_ALERT_TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        print('no Telegram secrets set — not sending')
        return False
    body = urllib.parse.urlencode({'chat_id': chat, 'text': text,
                                   'disable_web_page_preview': 'true'}).encode()
    request = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/sendMessage', data=body)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200
    except Exception as error:                      # noqa: BLE001 - never fail the run on this
        print(f'could not reach Telegram: {type(error).__name__}')
        return False


def main() -> int:
    dry_run = '--dry-run' in sys.argv
    try:
        all_failed, report = run_check()
    except subprocess.TimeoutExpired:
        all_failed, report = True, 'The check itself timed out after 3 minutes.'

    print(report)
    if not all_failed:
        print('\nat least one way works — the funnel is fine, saying nothing')
        return 0

    message = (
        'Unfollow Tracker — the username screen is DOWN\n\n'
        'Every way of looking up a profile failed, so new users cannot get '
        'past the first screen and the funnel earns nothing.\n\n'
        f'{report}\n\n'
        'The fix is usually a new recipe in Firebase Remote Config '
        '(key: ig_playbook) — no App Store update needed.'
    )
    print('\nALL WAYS FAILED' + (' — dry run, not sending' if dry_run else ' — alerting'))
    if dry_run:
        return 1
    tell_telegram(message)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
