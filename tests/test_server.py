from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import json
import os
import socket
import sqlite3
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from server.app import create_app
from server.delivery import TestAdapter, arm_test_jobs, step
from server.model import Failure, Lead, MIB, Settings, csv_cell, sheets_row
from server.photos import decode_photo
from server.store import Store, fingerprint


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('External network is forbidden in D4 tests')
    monkeypatch.setattr(socket, 'create_connection', deny)


@pytest.fixture
def clock():
    value = [1_800_000_000.0]
    return value


@pytest.fixture
def settings(tmp_path, clock):
    return Settings(data_dir=tmp_path / 'data', minute_limit=1000, hour_limit=1000, clock=lambda: clock[0])


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url='http://127.0.0.1:4344') as client:
        yield client


def picture(fmt='PNG', size=(40, 30), exif=False):
    out = BytesIO()
    meta = Image.Exif()
    if exif:
        meta[270] = 'private-test-metadata'
        meta[274] = 6
    Image.new('RGB', size, '#5f7251').save(out, format=fmt, **({'exif': meta} if exif else {}))
    return out.getvalue()


def fields(**changes):
    return dict(sofa='Прямой', size='Два места', material='Ткань / рогожка',
                description='Тестовый след чая.', channel='email', contact='demo@example.test', **{}) | changes


def send(client, key=None, data=None, photos=None, prefix='', headers=None):
    return client.post(prefix + '/api/submissions',
                       headers={'Idempotency-Key': key or str(uuid.uuid4())} | (headers or {}),
                       data=fields() if data is None else data,
                       files=[('photos', ('test.png', picture(), 'image/png'))] if photos is None else photos)


def count(store, table):
    assert table in {'submissions', 'photos', 'deliveries'}
    with store.connection() as c:
        return c.execute('SELECT count(*) FROM ' + table).fetchone()[0]


def get_receipt(client, accepted, **kwargs):
    return client.get('/api/receipts/' + accepted['submission_id'], headers={'X-Receipt-Key': accepted['receipt_key']}, **kwargs)


def test_acceptance_is_atomic_and_receipt_is_private(client, app):
    response = send(client)
    assert response.status_code == 201
    data = response.json()
    assert 'доставка не подключена' in data['message']
    store = app.state.store
    assert [count(store, t) for t in ('submissions', 'photos', 'deliveries')] == [1, 1, 2]
    receipt = get_receipt(client, data)
    assert receipt.status_code == 200
    assert {j['status'] for j in receipt.json()['channels']} == {'not_connected'}
    assert not any(j['simulated'] for j in receipt.json()['channels'])
    text = receipt.text
    assert 'demo@example.test' not in text and 'Тестовый след чая' not in text and 'receipt_key' not in text
    assert client.get('/api/receipts/' + data['submission_id']).status_code == 404
    assert client.get('/api/receipts/' + data['submission_id'], headers={'X-Receipt-Key': 'wrong'}).status_code == 404


@pytest.mark.parametrize('fmt,mime', [('JPEG', 'image/jpeg'), ('PNG', 'image/png'), ('WEBP', 'image/webp')])
def test_formats_reencoded_and_metadata_removed(client, app, fmt, mime):
    response = send(client, photos=[('photos', ('../../original.' + fmt.lower(), picture(fmt, exif=True), mime))])
    assert response.status_code == 201
    with app.state.store.connection() as c:
        photo = c.execute('SELECT name,content FROM photos').fetchone()
        payload = c.execute('SELECT payload FROM submissions').fetchone()[0]
    assert '/' not in photo['name'] and photo['name'].endswith('.jpg')
    assert 'original' not in payload
    with Image.open(BytesIO(photo['content'])) as im:
        im.load()
        assert im.format == 'JPEG' and not im.getexif() and 'icc_profile' not in im.info


@pytest.mark.parametrize('changes', [
    {'description': ''}, {'description': ' '}, {'description': 'x' * 1001},
    {'sofa': 'Чужое значение'}, {'size': ''}, {'material': ''},
    {'contact': 'real@example.com'}, {'channel': 'webhook'}, {'contact': 'demo@example.test\n'},
    {'channel': 'telegram', 'contact': '@real_person'},
])
def test_invalid_fields_are_rejected_without_reflection(client, app, changes):
    # Завершающие пробелы допустимо нормализуются; перевод строки внутри контакта недопустим.
    if changes.get('contact') == 'demo@example.test\n':
        changes = {'contact': 'demo@\nexample.test'}
    response = send(client, data=fields(**changes))
    assert response.status_code == 422
    assert count(app.state.store, 'submissions') == 0
    assert 'real@example.com' not in response.text


def test_missing_duplicate_unknown_fields_and_no_photo(client, app):
    data = fields(); del data['sofa']
    assert send(client, data=data).status_code == 422
    assert send(client, data=fields(webhook='http://127.0.0.1/private')).status_code in {400, 422}
    assert send(client, data=fields(sofa=['Прямой', 'Угловой'])).status_code in {400, 422}
    assert send(client, photos=[('wrong', ('a.png', picture(), 'image/png'))]).status_code == 422
    assert send(client, photos=[]).status_code == 415
    assert count(app.state.store, 'submissions') == 0


@pytest.mark.parametrize('content,mime', [
    (b'', 'image/png'), (b'not-an-image', 'image/png'), (b'<svg onload="alert(1)"></svg>', 'image/svg+xml'),
    (b'<html>test</html>', 'image/png'), (picture(), 'image/jpeg'),
    (picture('JPEG')[:100], 'image/jpeg'), (b'x' * (5 * MIB + 1), 'image/jpeg'),
])
def test_bad_photo_is_not_saved(client, app, content, mime):
    assert send(client, photos=[('photos', ('anything.jpg', content, mime))]).status_code == 422
    assert count(app.state.store, 'submissions') == 0


def test_photo_count_sum_and_pixels(client, app):
    assert send(client, photos=[('photos', ('a.png', picture(), 'image/png'))] * 4).status_code == 400
    padded = picture('JPEG') + b'\0' * (4 * MIB)
    assert send(client, photos=[('photos', ('a.jpg', padded, 'image/jpeg'))] * 3).status_code == 422
    assert send(client, photos=[('photos', ('a.png', picture(size=(5001, 4000)), 'image/png'))]).status_code == 422
    assert count(app.state.store, 'submissions') == 0


def test_decoder_timeout_is_bounded(monkeypatch):
    class Stuck:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def communicate(self, source, timeout):
            assert timeout == 6
            raise subprocess.TimeoutExpired('decoder', 6)
        def kill(self): pass
        def wait(self, timeout): return -9
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: Stuck())
    with pytest.raises(Failure) as failure:
        decode_photo(picture(), 'image/png')
    assert failure.value.status == 422


def test_body_limit_checks_header_and_chunked_body(client):
    headers = {'Idempotency-Key': str(uuid.uuid4()), 'Content-Type': 'multipart/form-data; boundary=abc'}
    assert client.post('/api/submissions', headers=headers | {'Content-Length': str(14 * MIB)}, content=b'').status_code == 413
    response = client.post('/api/submissions', headers=headers, content=iter([b'x' * MIB] * 14))
    assert response.status_code == 413


def test_same_key_replays_different_body_conflicts(client, app):
    key = str(uuid.uuid4())
    first = send(client, key).json()
    replay = send(client, key)
    assert replay.status_code == 200 and replay.json()['replayed']
    assert replay.json()['submission_id'] == first['submission_id']
    assert replay.json()['receipt_key'] == first['receipt_key']
    assert send(client, key, fields(description='Другой тест')).status_code == 409
    assert send(client, key, photos=[('photos', ('other.png', picture(size=(50, 50)), 'image/png'))]).status_code == 409
    assert count(app.state.store, 'submissions') == 1


def test_simultaneous_save_same_key_is_unique(client, app):
    store = app.state.store
    lead = Lead(**fields()); originals = [(picture(), 'image/png')]
    key, digest = str(uuid.uuid4()), fingerprint(lead, originals)
    clean = decode_photo(*originals[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: store.save(key, digest, lead, [clean]), range(2)))
    assert len({r['submission_id'] for r in responses}) == 1
    assert sorted(r['replayed'] for r in responses) == [False, True]
    assert count(store, 'deliveries') == 2


def test_restart_and_retry_after_lost_response(settings):
    key = str(uuid.uuid4())
    app = create_app(settings)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        accepted = send(client, key).json()  # сервер сохранил, клиент условно потерял ответ
    restarted = create_app(settings)
    with TestClient(restarted, base_url='http://127.0.0.1') as client:
        replay = send(client, key)
        assert replay.status_code == 200 and replay.json()['submission_id'] == accepted['submission_id']
        assert get_receipt(client, accepted).status_code == 200
        assert count(restarted.state.store, 'submissions') == 1


def test_sqlite_failure_rolls_back_submission_photos_jobs(client, app):
    store = app.state.store
    with store.connection() as c:
        c.execute("CREATE TRIGGER fail_job BEFORE INSERT ON deliveries WHEN NEW.channel='sheets' BEGIN SELECT RAISE(ABORT,'test-storage-failure'); END")
    response = send(client)
    assert response.status_code == 503 and 'test-storage-failure' not in response.text
    assert [count(store, t) for t in ('submissions', 'photos', 'deliveries')] == [0, 0, 0]


def test_error_after_commit_retries_safely(client, app, monkeypatch):
    original = app.state.store.save
    def uncertain(*args):
        original(*args)
        raise sqlite3.OperationalError('synthetic uncertainty')
    monkeypatch.setattr(app.state.store, 'save', uncertain)
    key = str(uuid.uuid4())
    assert send(client, key).status_code == 503
    replay = send(client, key)
    assert replay.status_code == 200 and replay.json()['replayed']
    assert count(app.state.store, 'submissions') == 1


def test_expired_and_other_receipts(client, clock):
    first, second = send(client).json(), send(client).json()
    assert client.get('/api/receipts/' + first['submission_id'], headers={'X-Receipt-Key': second['receipt_key']}).status_code == 404
    clock[0] += 3600
    assert get_receipt(client, first).status_code == 404


def test_rate_limit_survives_restart(settings, clock):
    settings.minute_limit = 2
    settings.hour_limit = 3
    with TestClient(create_app(settings), base_url='http://127.0.0.1') as client:
        assert send(client).status_code == 201
        assert send(client).status_code == 201
        limited = send(client)
        assert limited.status_code == 429 and limited.headers['retry-after'] == '60'
    with TestClient(create_app(settings), base_url='http://127.0.0.1') as client:
        assert send(client).status_code == 429
        clock[0] += 61
        assert send(client).status_code == 201
        assert send(client).status_code == 429  # часовой лимит


def test_storage_quota_no_partial_save(client, app):
    app.state.store.settings.storage_limit = 1
    assert send(client).status_code == 507
    assert count(app.state.store, 'submissions') == 0


def test_private_paths_origin_host_and_no_list(client):
    for path in ['/api/submissions', '/data/registry.sqlite3', '/uploads/a.jpg', '/.env', '/server/app.py']:
        assert client.get(path).status_code in {404, 405}
    assert client.get('/api/health', headers={'host': 'evil.example'}).status_code == 400
    assert send(client, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert send(client, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert send(client, headers={'Origin': 'http://127.0.0.1:4344'}).status_code == 201
    assert 'no-store' in client.get('/').headers['cache-control']
    assert "connect-src 'self'" in client.get('/').headers['content-security-policy']


def test_prefix(settings):
    settings.prefix = '/d4'
    with TestClient(create_app(settings), base_url='http://127.0.0.1') as client:
        assert client.get('/d4/').status_code == 200
        assert client.get('/d4/style.css').status_code == 200
        accepted = send(client, prefix='/d4')
        assert accepted.status_code == 201
        d = accepted.json()
        assert client.get('/d4/api/receipts/' + d['submission_id'], headers={'X-Receipt-Key': d['receipt_key']}).status_code == 200
        assert client.get('/api/health').status_code == 404


def test_cleanup_cascades_and_idempotency_retention(client, app, clock):
    key = str(uuid.uuid4())
    old = send(client, key).json()
    clock[0] += 86400
    assert app.state.store.cleanup(False) == 1 and count(app.state.store, 'submissions') == 1
    assert app.state.store.cleanup(True) == 1
    assert [count(app.state.store, t) for t in ('submissions', 'photos', 'deliveries')] == [0, 0, 0]
    assert get_receipt(client, old).status_code == 404
    assert send(client, key).json()['submission_id'] != old['submission_id']


def test_backup_restore_and_permissions(client, app, tmp_path):
    store = app.state.store
    accepted = send(client).json()
    backup = tmp_path / 'backup' / 'snapshot.sqlite3'
    store.backup(backup)
    assert backup.stat().st_mode & 0o777 == 0o600
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.settings.data_dir.stat().st_mode & 0o777 == 0o700
    send(client)
    store.restore(backup)
    assert count(store, 'submissions') == 1 and get_receipt(client, accepted).status_code == 200
    with pytest.raises(ValueError):
        store.backup(backup)


def test_channel_failure_independence_retry_limits_and_unknown(client, app, clock):
    accepted = send(client).json()
    store = app.state.store
    arm_test_jobs(store)
    assert step(store, 'telegram', TestAdapter('unavailable')) == 'pending'
    assert step(store, 'sheets', TestAdapter('success')) == 'delivered'
    assert step(store, 'telegram', TestAdapter('success')) is None  # рано повторять
    for expected in ['pending', 'failed']:
        clock[0] += 31
        assert step(store, 'telegram', TestAdapter('unavailable')) == expected
    assert step(store, 'telegram', TestAdapter('success')) is None
    with store.connection() as c:
        rows = c.execute('SELECT channel,status,attempts,simulated FROM deliveries').fetchall()
    assert {r['channel']: r['attempts'] for r in rows} == {'telegram': 3, 'sheets': 1}
    assert all(r['simulated'] for r in rows)
    assert 'доставка не подключена' in get_receipt(client, accepted).json()['message']


@pytest.mark.parametrize('scenario,state', [('timeout', 'unknown'), ('forbidden', 'failed'), ('rate_limit', 'pending')])
def test_delivery_scenarios(client, app, clock, scenario, state):
    send(client)
    store = app.state.store
    arm_test_jobs(store)
    assert step(store, 'telegram', TestAdapter(scenario)) == state
    clock[0] += 30
    assert step(store, 'telegram', TestAdapter('success')) is None
    if scenario == 'rate_limit':
        clock[0] += 31
        assert step(store, 'telegram', TestAdapter('success')) == 'delivered'
    if scenario == 'timeout':
        clock[0] += 3600
        arm_test_jobs(store)
        assert step(store, 'telegram', TestAdapter('success')) is None


def test_worker_interruption_recovers_as_unknown(client, app):
    send(client)
    store = app.state.store
    with store.connection() as c:
        c.execute("UPDATE deliveries SET status='processing',simulated=1 WHERE channel='telegram'")
    store.initialize()
    with store.connection() as c:
        assert c.execute("SELECT status FROM deliveries WHERE channel='telegram'").fetchone()[0] == 'unknown'
    assert step(store, 'telegram', TestAdapter('success')) is None


def test_plain_text_and_formula_defences(client, app):
    description = '=IMPORTXML("https://example.test")<script>demo</script>'
    assert send(client, data=fields(description=description)).status_code == 201
    with app.state.store.connection() as c:
        assert json.loads(c.execute('SELECT payload FROM submissions').fetchone()[0])['description'] == description
    lead = Lead(**fields(description=description))
    assert sheets_row('synthetic-id', lead)[4] == description  # RAW, не формула
    assert csv_cell('  =1+1').startswith("'")
    assert csv_cell('@demo_test').startswith("'")
    assert csv_cell('Обычный текст') == 'Обычный текст'


def test_public_data_path_forbidden(tmp_path):
    from server.model import ROOT
    with pytest.raises(ValueError):
        Settings(data_dir=ROOT / 'public' / 'data')


@pytest.mark.parametrize('ps_error', [False, True])
def test_memory_watchdog_kills_over_budget_or_unobservable_child(monkeypatch, ps_error):
    import threading
    from server.photos import monitor_memory
    class Child:
        pid = 12345
        killed = False
        def poll(self): return None
        def kill(self): self.killed = True
    child = Child()
    def reading(*args, **kwargs):
        assert args[0] == ['/bin/ps', '-o', 'rss=', '-p', '12345']
        if ps_error:
            raise PermissionError('synthetic denied')
        return str(400 * 1024).encode()
    monkeypatch.setattr(subprocess, 'check_output', reading)
    failed = threading.Event()
    monitor_memory(child, threading.Event(), failed)
    assert child.killed and failed.is_set()


def test_partial_request_disconnect_does_not_save(client, app):
    import asyncio
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'POST', 'scheme': 'http', 'path': '/api/submissions', 'raw_path': b'/api/submissions',
             'query_string': b'', 'root_path': '', 'server': ('127.0.0.1', 4344), 'client': ('127.0.0.1', 1234),
             'headers': [(b'host', b'127.0.0.1:4344'), (b'content-type', b'multipart/form-data; boundary=abc'),
                         (b'idempotency-key', str(uuid.uuid4()).encode())]}
    messages = iter([{'type': 'http.request', 'body': b'--abc\r\n', 'more_body': True}, {'type': 'http.disconnect'}])
    sent = []
    async def receive(): return next(messages)
    async def collect(message): sent.append(message)
    asyncio.run(app(scope, receive, collect))
    assert next(m['status'] for m in sent if m['type'] == 'http.response.start') == 408
    assert count(app.state.store, 'submissions') == 0


def test_http_acceptance_never_calls_delivery_adapter(client, monkeypatch):
    def forbidden(*args): raise AssertionError('Delivery must not run in request')
    monkeypatch.setattr(TestAdapter, 'send', forbidden)
    assert send(client).status_code == 201


def test_admin_commands_refuse_running_server(client, settings, monkeypatch):
    from server import manage
    monkeypatch.setattr(manage, 'Settings', lambda: settings)
    monkeypatch.setattr(sys, 'argv', ['manage', 'backup'])
    with pytest.raises(SystemExit) as stopped:
        manage.main()
    assert stopped.value.code == 2


def test_automatic_retention_cleanup(settings, clock):
    import time
    settings.cleanup_interval = 0.02
    app = create_app(settings)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert send(client).status_code == 201
        clock[0] += 86401
        for _ in range(30):
            if count(app.state.store, 'submissions') == 0:
                break
            time.sleep(0.02)
        assert count(app.state.store, 'submissions') == 0


def test_cli_cleanup_expired_backups_and_rows(settings, clock, tmp_path, monkeypatch):
    from server import manage
    store = Store(settings); store.initialize()
    lead = Lead(**fields())
    store.save(str(uuid.uuid4()), 'synthetic-hash', lead, [decode_photo(picture(), 'image/png')])
    backups = tmp_path / 'backups'; backups.mkdir()
    old = backups / 'old.sqlite3'; old.write_bytes(b'synthetic old backup')
    fresh = backups / 'fresh.sqlite3'; fresh.write_bytes(b'synthetic fresh backup')
    clock[0] += 86401
    os.utime(old, (clock[0] - 86401,) * 2)
    os.utime(fresh, (clock[0],) * 2)
    monkeypatch.setattr(manage, 'ROOT', tmp_path)
    monkeypatch.setattr(manage, 'Settings', lambda: settings)
    monkeypatch.setattr(sys, 'argv', ['manage', 'cleanup', '--apply'])
    manage.main()
    assert not old.exists() and fresh.exists() and count(store, 'submissions') == 0
