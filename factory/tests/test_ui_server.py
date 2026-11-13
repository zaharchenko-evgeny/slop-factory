"""The HTTP surface serves only packaged assets and a redacted read-only snapshot."""
import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from factory_ui.server import make_server
from factory_ui.data import read_gh, public


@pytest.fixture
def server(tmp_path):
    (tmp_path / 'index.html').write_text('<title>Factory progress</title>')
    class Store:
        calls = 0
        sources = []
        def snapshot(self, older=False, progress=None):
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
    body = json.load(urllib.request.urlopen(url + '/api/snapshot?older=1'))
    assert body == {'snapshot': None, 'snapshotRunId': None,
                    'refresh': {'id': 0, 'state': 'idle', 'older': True,
                                'startedAt': None, 'finishedAt': None, 'error': None,
                                'sources': []}}
    assert store.calls == 0


def wait_for_state(url, path, state):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        body = json.load(urllib.request.urlopen(url + path, timeout=1))
        refresh = body['refresh'] if 'refresh' in body else None
        if refresh and refresh['state'] == state:
            return body
        time.sleep(0.01)
    raise AssertionError(f'refresh did not reach {state}')


def make_blocking_store():
    class Store:
        sources = [SimpleNamespace(id='source', name='Source')]

        def __init__(self):
            self.calls = []
            self.entered = threading.Event()
            self.allow_ticket = threading.Event()
            self.ticket_done = threading.Event()
            self.finish = threading.Event()
            self.fail_next = False
            self.block_first_current = True

        def snapshot(self, older=False, progress=None):
            self.calls.append(older)
            source = self.sources[0]
            if progress:
                progress(source, 'listing')
                progress(source, 'listing', total_tickets=2)
                progress(source, 'prefetch', total_tickets=2)
                progress(source, 'events', total_tickets=2)
                progress(source, 'tickets', total_tickets=2)
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError('secret token in exception text')
            if not older and self.block_first_current:
                self.block_first_current = False
                self.entered.set()
                assert self.allow_ticket.wait(3)
                if progress:
                    progress(source, 'tickets', completed_tickets=1, total_tickets=2)
                self.ticket_done.set()
                assert self.finish.wait(3)
                if progress:
                    progress(source, 'tickets', completed_tickets=2, total_tickets=2)
            if progress:
                progress(source, 'complete', error_count=0)
            return {'older': older, 'call': len(self.calls)}

    return Store()


def test_refresh_is_async_coalesced_queued_and_status_is_cached(tmp_path):
    (tmp_path / 'index.html').write_text('ui')
    store = make_blocking_store()
    http = make_server(store, static=tmp_path)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{http.server_port}'
    try:
        started = json.load(urllib.request.urlopen(url + '/api/snapshot?refresh=1', timeout=1))
        run_id = started['refresh']['id']
        assert run_id > 0
        assert store.entered.wait(1), 'background tracker worker did not start'

        duplicate = json.load(urllib.request.urlopen(url + '/api/snapshot?refresh=1', timeout=1))
        assert duplicate['refresh']['id'] == run_id
        queued = json.load(urllib.request.urlopen(url + '/api/snapshot?older=1&refresh=1', timeout=1))
        assert queued['refresh']['state'] == 'queued'
        older_run_id = queued['refresh']['id']
        assert older_run_id != run_id

        cached = json.load(urllib.request.urlopen(url + '/api/snapshot', timeout=1))
        status = json.load(urllib.request.urlopen(url + '/api/status', timeout=1))
        assert cached['snapshot'] is None
        assert status['snapshotRunId'] is None
        assert status['refresh']['state'] == 'running'
        assert status['refresh']['sources'][0]['phase'] == 'tickets'
        assert status['refresh']['sources'][0]['totalTickets'] == 2
        assert store.calls == [False]

        store.allow_ticket.set()
        assert store.ticket_done.wait(1)
        progressed = json.load(urllib.request.urlopen(url + '/api/status', timeout=1))
        assert progressed['refresh']['sources'][0]['completedTickets'] == 1
        store.finish.set()

        completed = wait_for_state(url, '/api/status', 'complete')
        assert completed['snapshotRunId'] == run_id
        older_complete = wait_for_state(url, '/api/status?older=1', 'complete')
        assert older_complete['snapshotRunId'] == older_run_id
        assert store.calls == [False, True]
        assert json.load(urllib.request.urlopen(url + '/api/snapshot'))['snapshot'] == {
            'older': False, 'call': 1}
    finally:
        store.allow_ticket.set()
        store.finish.set()
        http.shutdown()
        http.server_close()
        thread.join()


def test_failed_refresh_retains_snapshot_and_allows_retry(tmp_path):
    (tmp_path / 'index.html').write_text('ui')
    store = make_blocking_store()
    store.block_first_current = False
    http = make_server(store, static=tmp_path)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{http.server_port}'
    try:
        first = json.load(urllib.request.urlopen(url + '/api/snapshot?refresh=1'))
        first_run = first['refresh']['id']
        wait_for_state(url, '/api/status', 'complete')
        original = json.load(urllib.request.urlopen(url + '/api/snapshot'))
        assert original['snapshotRunId'] == first_run

        store.fail_next = True
        failed_start = json.load(urllib.request.urlopen(url + '/api/snapshot?refresh=1'))
        failed = wait_for_state(url, '/api/status', 'failed')
        assert failed['refresh']['id'] == failed_start['refresh']['id']
        assert 'secret token' not in failed['refresh']['error']
        retained = json.load(urllib.request.urlopen(url + '/api/snapshot'))
        assert retained['snapshot'] == original['snapshot']
        assert retained['snapshotRunId'] == first_run

        retry = json.load(urllib.request.urlopen(url + '/api/snapshot?refresh=1'))
        retried = wait_for_state(url, '/api/status', 'complete')
        assert retried['snapshotRunId'] == retry['refresh']['id']
        assert store.calls == [False, False, False]
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


@pytest.mark.parametrize('path,headers,method,code', [
    ('/api/snapshot', {'Host': 'evil.test'}, 'GET', 403),
    ('/api/snapshot', {'Origin': 'https://evil.test'}, 'GET', 403),
    ('/api/snapshot', {'Sec-Fetch-Site': 'cross-site'}, 'GET', 403),
    ('/api/snapshot', {}, 'POST', 405),
    ('/api/snapshot', {}, 'DELETE', 405),
    ('/api/snapshot?command=merge', {}, 'GET', 400),
    ('/api/snapshot?refresh=0', {}, 'GET', 400),
    ('/api/status?older=', {}, 'GET', 400),
    ('/api/status?refresh=1', {}, 'GET', 400),
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


def test_status_redacts_process_secrets_in_source_names(monkeypatch, tmp_path):
    secret = 'highly-sensitive-test-value'
    monkeypatch.setenv('CUSTOM_TOKEN', secret)
    (tmp_path / 'index.html').write_text('ui')

    class Store:
        sources = [SimpleNamespace(id='source', name=f'Source {secret}')]

    http = make_server(Store(), static=tmp_path)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{http.server_port}/api/status') as response:
            raw = response.read()
        assert secret.encode() not in raw
        assert json.loads(raw)['refresh']['sources'][0]['name'] == 'Source [redacted]'
    finally:
        http.shutdown()
        http.server_close()
        thread.join()
