"""Offline repository/history inventory. Never emit matched credentials or raw lines.

Run from any directory. Exact environment-secret matches and recognizable credential
formats are evidence; entropy/assignment matches need human triage. The scanner cannot
establish that every possible secret format is absent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

REPO = Path(__file__).resolve().parents[4]
PATTERNS = {
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github_token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})'),
    'aws_access_id': re.compile(rb'AKIA[A-Z0-9]{16}'),
    'slack_token': re.compile(rb'xox[baprs]-[A-Za-z0-9-]{20,}'),
    'agentpal_token': re.compile(rb'ap[ok]_[A-Za-z0-9]{16,}_[A-Za-z0-9_-]{32,}'),
    'secret_assignment': re.compile(
        rb'''(?i)(?:client_secret|api_key|access_token|password)\s*["']?\s*[:=]\s*["'][A-Za-z0-9_+/=-]{32,}["']'''),
}


def scan():
    # Buyer email and client IDs are identifiers; scan and report them separately
    # from secrets. The known public sandbox persona email is intentionally tracked.
    configured = {k: v.encode() for k, v in os.environ.items() if len(v) >= 12 and (
        k.startswith(('PAYPAL_SANDBOX_', 'CHANNEL3_', 'KERNEL_')) or
        k.endswith(('API_KEY', 'CLIENT_SECRET', 'BUYER_PASSWORD')))}
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=REPO).decode().split('\0')
    hits, exact = [], []
    for name in paths:
        path = REPO / name
        if not name or not path.is_file():
            continue
        raw = path.read_bytes()
        for kind, pattern in PATTERNS.items():
            for match in pattern.finditer(raw):
                hits.append({'path': name, 'line': raw[:match.start()].count(b'\n') + 1,
                             'kind': kind})
        for key, value in configured.items():
            if value in raw:
                exact.append({'path': name, 'credential_name': key})
    history, historical_exact, commit, path = [], [], None, None
    # This is deliberately git log -p, including deleted historical material.
    with subprocess.Popen(['git', 'log', '--all', '-p', '--no-ext-diff', '--format=commit:%H'],
                          cwd=REPO, stdout=subprocess.PIPE) as process:
        for line in process.stdout:
            if line.startswith(b'commit:'):
                commit = line.decode().strip()[7:19]
            elif line.startswith(b'+++ b/'):
                path = line.decode(errors='replace').strip()[6:]
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    history.append({'commit': commit, 'path': path, 'kind': kind})
            for key, value in configured.items():
                if value in line:
                    historical_exact.append({'commit': commit, 'path': path, 'credential_name': key})
        if process.wait() != 0:
            raise RuntimeError('history scan failed')
    return {'tracked_files': len(paths) - 1, 'configured_values_checked': len(configured),
            'pattern_matches': hits, 'configured_matches': exact,
            'history_pattern_matches': history, 'history_configured_matches': historical_exact}


if __name__ == '__main__':
    print(json.dumps(scan(), indent=2))
