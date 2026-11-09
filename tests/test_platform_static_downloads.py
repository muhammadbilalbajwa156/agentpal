"""Only the two committed JSON documents may be downloaded through the dashboard."""
from pathlib import Path

import pytest

from agentpal.dashboard import load_site
from test_hosted_render import hosted, via_render  # noqa: F401


@pytest.mark.parametrize('name,path', [
    ('openapi.json', 'docs/api/openapi.json'),
    ('agentpal.postman_collection.json', 'postman/v1.collection.json'),
])
def test_committed_download_is_served_exactly(hosted, name, path):  # noqa: F811
    _, c = hosted
    r = c.get('/app/' + name, headers=via_render())
    assert r.status_code == 200
    assert r.headers['content-type'] == 'application/json'
    assert r.content == (Path(__file__).resolve().parents[1] / path).read_bytes()
    assert r.headers['x-content-type-options'] == 'nosniff'
    head = c.head('/app/' + name, headers=via_render())
    assert head.status_code == 200 and head.content == b''
    assert c.post('/app/' + name, headers=via_render()).status_code == 405


def test_site_directory_cannot_supply_arbitrary_json(tmp_path):
    (tmp_path / 'index.html').write_text('index')
    for name in ('secrets.json', 'openapi.json', 'agentpal.postman_collection.json'):
        (tmp_path / name).write_text('{"token":"must not serve"}')
    assert set(load_site(tmp_path)) == {'index.html'}


@pytest.mark.parametrize('unsafe', ['missing', 'symlink', 'oversized'])
def test_downloads_reject_unsafe_sources_at_startup(tmp_path, monkeypatch, unsafe):
    import agentpal.dashboard as dashboard
    site = tmp_path / 'site'
    site.mkdir()
    (site / 'index.html').write_text('index')
    artifact = tmp_path / 'committed.json'
    if unsafe == 'symlink':
        target = tmp_path / 'target.json'
        target.write_text('{}')
        artifact.symlink_to(target)
    elif unsafe == 'oversized':
        artifact.write_bytes(b'x' * (dashboard.MAX_FILE_BYTES + 1))
    monkeypatch.setattr(dashboard, 'SITE_DIR', site)
    monkeypatch.setattr(dashboard, 'DOWNLOADS', {'openapi.json': artifact})
    with pytest.raises(ValueError):
        dashboard.load_site(site)
