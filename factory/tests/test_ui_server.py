"""The HTTP surface serves only packaged assets and a redacted read-only snapshot."""
import json
import threading
import urllib.error
import urllib.request

import pytest
from factory_ui.server import make_server
from factory_ui.data import read_gh, public


@pytest.fixture
def server(tmp_path):
    (tmp_path / 'index.html').write_text('<title>Factory progress</title>')
    class Store:
        calls = 0
        def snapshot(self, older=False):
            self.calls += 1
            return {'older': older}
    store = Store()
    http = make_server(store, static=tmp_path)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{http.server_port}', store
    http.shutdown()
    http.server_close()
    thread.join()


def test_ui_loopback_assets_and_snapshot(server):
    url, store = server
    with urllib.request.urlopen(url) as response:
        assert response.status == 200
        assert b'Factory progress' in response.read()
        assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
        assert response.headers.get('Access-Control-Allow-Origin') is None
    assert json.load(urllib.request.urlopen(url + '/api/snapshot?older=1')) == {'older': True}
    assert store.calls == 1


@pytest.mark.parametrize('path,headers,method,code', [
    ('/api/snapshot', {'Host': 'evil.test'}, 'GET', 403),
    ('/api/snapshot', {'Origin': 'https://evil.test'}, 'GET', 403),
    ('/api/snapshot', {'Sec-Fetch-Site': 'cross-site'}, 'GET', 403),
    ('/api/snapshot', {}, 'POST', 405),
    ('/api/snapshot', {}, 'DELETE', 405),
    ('/api/snapshot?command=merge', {}, 'GET', 400),
    ('/%2e%2e/data.py', {}, 'GET', 404),
    ('/api/merge', {}, 'GET', 404),
])
def test_ui_rejects_cross_origin_writes_and_paths(server, path, headers, method, code):
    url, store = server
    req = urllib.request.Request(url + path, headers=headers, method=method)
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(req)
    assert error.value.code == code
    assert store.calls == 0


@pytest.mark.parametrize('args', [
    ['api', '--method=DELETE', 'repos/a/b/issues/1'], ['api', '-XPOST', 'repos/a/b/issues'],
    ['api', 'graphql', '-f', 'query=query { viewer { login } } mutation M { deleteIssue }', '-f', 'operationName=M'],
    ['issue', 'edit', '1'], ['pr', 'merge', '1'], ['api', '-X', 'POST', 'repos/a/b/issues'],
    ['api', 'graphql', '-f', 'query=mutation {deleteIssue}'],
    ['api', 'repos/a/b/issues', '-f', 'title=write'],
    ['api', 'repos/a/b/issues', '--field=title=write'],
    ['api', 'repos/a/b/issues', '--raw-field=title=write'],
    ['api', 'repos/a/b/issues', '-fbody=write'],
    ['api', 'repos/a/b/issues', '-Fbody=write'],
])
def test_ui_gh_capability_refuses_writes(args):
    with pytest.raises(PermissionError):
        read_gh(args)


def test_ui_serialization_omits_process_secrets(monkeypatch):
    monkeypatch.setenv('CUSTOM_TOKEN', 'highly-sensitive-test-value')
    assert public({'reason': 'source failed highly-sensitive-test-value'}) == {'reason': 'source failed [redacted]'}
