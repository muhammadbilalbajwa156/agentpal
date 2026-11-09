"""Offline lockfile hygiene; no package installation or advisory-service traffic."""
import json
from pathlib import Path
import re

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('filename', ['requirements.lock'])
def test_python_locks_pin_every_entry_and_include_sha256(filename):
    contents = (PROJECT / filename).read_text()
    # Join pip's continued lines; comments may span additional standalone lines.
    entries = [line.strip() for line in contents.replace('\\\n', ' ').splitlines()
               if line.strip() and not line.lstrip().startswith('#')]
    assert entries
    for entry in entries:
        requirement = entry.split('--hash=', 1)[0].strip()
        parsed = Requirement(requirement)
        assert parsed.url is None
        assert len(parsed.specifier) == 1
        assert next(iter(parsed.specifier)).operator == '=='
        assert re.search(r'--hash=sha256:[a-f0-9]{64}(?:\s|$)', entry)
        assert not any(flag in entry for flag in ('--trusted-host', '--extra-index-url', 'git+'))


def test_all_direct_runtime_dependencies_are_present_at_compatible_locked_versions():
    contents = (PROJECT / 'requirements.lock').read_text()
    pins = {canonicalize_name(name): version for name, version in
            re.findall(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)', contents, re.M)}
    direct = [Requirement(line) for line in (PROJECT / 'requirements.txt').read_text().splitlines()
              if line.strip() and not line.startswith('#')]
    for dependency in direct:
        name = canonicalize_name(dependency.name)
        assert name in pins
        assert pins[name] in dependency.specifier


@pytest.mark.parametrize('directory', ['postman', 'apimatic'])
def test_npm_locks_use_integrity_and_https_registry(directory):
    lock = json.loads((PROJECT / directory / 'package-lock.json').read_text())
    assert lock['lockfileVersion'] >= 2
    dependencies = [value for name, value in lock['packages'].items() if name]
    assert dependencies
    for dependency in dependencies:
        if 'resolved' in dependency:
            assert dependency['resolved'].startswith('https://registry.npmjs.org/')
            assert dependency['integrity'].startswith(('sha512-', 'sha256-'))
        else:
            # npm can omit registry URL/integrity for in-bundle dependencies.
            assert dependency.get('inBundle') is True
        assert dependency['version']
