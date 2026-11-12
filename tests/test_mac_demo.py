"""Разовый путь Mac на искусственном ключе и подставных сервисах."""
import base64
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
import json
import logging
import os
import platform
import socket
import subprocess
from urllib.parse import parse_qs
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from server import closed, sheets_config
from server.app import create_app
from server.mac_auth import MacAuthorization, OPENSSL
from server.model import Failure, Lead, Settings
from server.sheets import row_for_submission, send_one as send_sheet
from server.store import Store
from server.telegram import send_one as send_telegram
from server.telegram_config import ConfigurationError
from tools import run
from test_closed_delivery import launch, private_file
from test_linked_delivery import states, write_examples
from test_server import fields, picture, send, get_receipt
from test_sheets import ACCOUNT, MARKER, SHEET_ID, Table, error
from test_telegram import TOKEN, CHAT, config_text, response

MAC_HEADER = {'X-D4-Delivery-Mode': 'mac_demo_live'}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('External network forbidden')
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(httpx, 'HTTPTransport', deny)


@pytest.fixture(scope='module')
def pem():
    result = subprocess.run([OPENSSL, 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048'],
                            capture_output=True, timeout=15, check=True)
    return result.stdout


def verify_signature(pem, assertion):
    public = subprocess.run([OPENSSL, 'pkey', '-pubout'], input=pem, capture_output=True, timeout=5, check=True).stdout
    body, signature = assertion.rsplit('.', 1)
    signature = base64.urlsafe_b64decode(signature + '=' * (-len(signature) % 4))
    public_fd, public_write = os.pipe()
    sign_fd, sign_write = os.pipe()
    try:
        os.write(public_write, public)
        os.write(sign_write, signature)
        os.close(public_write)
        os.close(sign_write)
        result = subprocess.run([OPENSSL, 'dgst', '-sha256', '-verify', f'/dev/fd/{public_fd}',
                                 '-signature', f'/dev/fd/{sign_fd}'], input=body.encode(), capture_output=True,
                                pass_fds=(public_fd, sign_fd), timeout=5)
        assert result.returncode == 0
    finally:
        os.close(public_fd)
        os.close(sign_fd)
    return [json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))) for part in body.split('.')]


@pytest.fixture
def mac_files(tmp_path, pem, monkeypatch):
    if platform.system() != 'Darwin':
        pytest.skip('Actual Mac runtime required')
    config_root = tmp_path / 'private-config'
    key = config_root / '.secrets/sheets-service-account.json'
    key.parent.mkdir(parents=True, mode=0o700)
    private_file(config_root / '.env', config_text(True))
    private_file(config_root / '.env.sheets', f'D4_SHEETS_SPREADSHEET_ID={SHEET_ID}\n'
                 f'D4_SHEETS_SERVICE_ACCOUNT={ACCOUNT}\nD4_SHEETS_KEY_FILE={key}\nD4_SHEETS_LIVE=1\n')
    private_file(key, json.dumps({'type': 'service_account', 'private_key': pem.decode(),
                 'private_key_id': 'synthetic-mac-key', 'client_email': ACCOUNT, 'token_uri': sheets_config.TOKEN_URI}))
    monkeypatch.setattr(closed, 'ROOT', config_root)
    monkeypatch.setattr(sheets_config, 'KEY_FILE', key)
    return config_root


@pytest.fixture
def providers(mac_files, pem, monkeypatch):
    table, messages, assertions, faults = Table(), [], [], {}

    def route(request):
        if str(request.url) == sheets_config.TOKEN_URI:
            form = parse_qs(request.content.decode())
            assert request.method == 'POST'
            assert form['grant_type'] == ['urn:ietf:params:oauth:grant-type:jwt-bearer']
            assertion, = form['assertion']
            header, claims = verify_signature(pem, assertion)
            assert header == {'alg': 'RS256', 'typ': 'JWT', 'kid': 'synthetic-mac-key'}
            assert claims['aud'] == sheets_config.TOKEN_URI and claims['scope'] == sheets_config.SCOPE
            assert claims['iss'] == ACCOUNT and claims['exp'] - claims['iat'] == 300
            assert set(claims) == {'iss', 'scope', 'aud', 'iat', 'exp'}
            assertions.append(assertion)
            logging.getLogger('httpx').warning('Synthetic assertion %s', assertion)
            return httpx.Response(faults.get('oauth', 200), json={'access_token': MARKER, 'token_type': 'Bearer', 'expires_in': 3600})
        if request.url.host == 'api.telegram.org':
            assert request.url.path == '/bot' + TOKEN + '/sendPhoto'
            parts = BytesParser(policy=policy.default).parsebytes(b'Content-Type: '
                + request.headers['content-type'].encode() + b'\r\n\r\n' + request.content)
            form = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
                    for part in parts.iter_parts()}
            assert form['chat_id'].decode() == CHAT
            messages.append((form['caption'].decode(), form['photo']))
            logging.getLogger('httpx').warning('Synthetic Telegram URL %s', request.url)
            if faults.get('telegram') == 'timeout':
                raise httpx.ReadTimeout('Synthetic response lost ' + TOKEN)
            return response(faults.get('telegram', 200))
        return table(request)

    def transport(**options):
        assert options == {'retries': 0, 'trust_env': False}
        return httpx.MockTransport(route)

    monkeypatch.setattr(httpx, 'HTTPTransport', transport)
    return table, messages, assertions, faults


@pytest.mark.parametrize(('fault', 'telegram', 'sheets', 'writes'), [
    ('success', 'live_delivered', 'live_delivered', 1),
    ('telegram403', 'live_failed', 'live_delivered', 1),
    ('sheets403', 'live_delivered', 'live_failed', 0),
    ('telegram429', 'live_failed', 'live_delivered', 1),
    ('sheets429', 'live_delivered', 'live_failed', 0),
    ('telegram_timeout', 'live_unknown', 'live_delivered', 1),
    ('sheets_timeout', 'live_delivered', 'live_unknown', 1),
    ('oauth429', 'live_delivered', 'live_failed', 0),
])
def test_one_mac_submission(tmp_path, providers, pem, launch, monkeypatch, caplog, capsys, fault, telegram, sheets, writes):
    table, messages, assertions, faults = providers
    if fault.startswith('telegram'):
        faults['telegram'] = {'telegram403': 403, 'telegram429': 429, 'telegram_timeout': 'timeout'}[fault]
    if fault.startswith('sheets'):
        if fault == 'sheets_timeout':
            def lost_response(request, table):
                assert len(table.rows) == 1
                raise httpx.ReadTimeout('Synthetic append response lost ' + MARKER)
            table.faults['append'] = lost_response
        else:
            table.faults['header'] = error(403 if fault == 'sheets403' else 429, retry=45)
    if fault == 'oauth429':
        faults['oauth'] = 429
    registry = tmp_path / 'run'
    app = launch(['--mac-demo-live', '--registry-dir', str(registry)])
    key = str(uuid.uuid4())
    with caplog.at_level(logging.DEBUG), TestClient(app, base_url='http://127.0.0.1') as client:
        assert client.get('/api/health').json()['delivery'] == 'mac_demo_live'
        page = client.get('/').text
        assert 'data-form-mode="server"' in page
        assert 'Это локальный демонстрационный стенд.' in page and 'до 24 часов.' in page
        assert 'Доставка в тестовый Telegram и тестовую Google-таблицу.' in page
        assert 'Сохранения на сервере нет.' not in page
        for headers in ({}, {'X-D4-Delivery-Mode': 'closed_live'}, {'X-D4-Delivery-Mode': 'not_connected'}):
            rejected = send(client, key, headers=headers)
            assert rejected.status_code == 409 and rejected.json()['delivery_mode'] == 'mac_demo_live'
        assert not messages and not assertions and not table.calls
        posted = send(client, key, headers=MAC_HEADER)
        assert posted.status_code == 201
        accepted = posted.json()
        assert set(states(accepted).values()) == {'live_queued'}
        receipt = get_receipt(client, accepted).json()
        assert states(receipt) == {'telegram': telegram, 'sheets': sheets}
        replay = send(client, key, headers=MAC_HEADER)
        assert replay.status_code == 200 and replay.json()['submission_id'] == accepted['submission_id']
        assert send(client, headers=MAC_HEADER).status_code == 409
    def forbidden():
        pytest.fail('Repeated launch read secrets')
    monkeypatch.setattr(run, 'build_mac_demo_delivery', forbidden)
    with pytest.raises(SystemExit) as rejected:
        launch(['--mac-demo-live', '--registry-dir', str(registry)])
    assert rejected.value.code == 2
    with TestClient(launch(['--registry-dir', str(registry)]), base_url='http://127.0.0.1') as client:
        assert get_receipt(client, accepted).json() == receipt
        assert send(client, key).json()['submission_id'] == accepted['submission_id']
    assert len(messages) == len(assertions) == 1 and table.append_count == writes
    caption, photo = messages[0]
    assert accepted['submission_id'] in caption
    assert 'demo@example.test' not in caption and 'Тестовый след чая' not in caption
    assert all(value in caption for value in ('Прямой', 'Два места', 'Ткань / рогожка'))
    with app.state.store.connection() as connection:
        assert photo == connection.execute('SELECT content FROM photos').fetchone()[0]
        assert photo.startswith(b'\xff\xd8')
        assert [tuple(row) for row in connection.execute('SELECT attempts,simulated FROM deliveries')] == [(1, 0), (1, 0)]
        assert table.rows == ([row_for_submission(app.state.store, accepted['submission_id'], connection)] if writes else [])
    if sheets == 'live_delivered':
        assert table.calls == ['header', 'scan', 'append', 'readback']
        assert len(table.rows[0]) == 9 and table.rows[0][7] == 'demo@example.test'
    captured = capsys.readouterr()
    visible = caplog.text + captured.out + captured.err + json.dumps([accepted, receipt])
    assert not any(secret in visible for secret in (TOKEN, CHAT, MARKER, ACCOUNT, SHEET_ID, pem.decode(), *assertions))
    assert accepted['receipt_key'] not in json.dumps(receipt) + caplog.text
    write_examples(tmp_path, **{fault: receipt})


def test_mac_guard_and_fixed_limit_precede_secret_loading(tmp_path, monkeypatch, capsys):
    def forbidden(*args):
        pytest.fail('Secrets were loaded')
    monkeypatch.setattr(closed.telegram_config, 'load_config', forbidden)
    monkeypatch.setattr(closed.platform, 'system', lambda: 'Linux')
    with pytest.raises(ConfigurationError, match='требует Mac'):
        closed.build_mac_demo_delivery()
    with pytest.raises(SystemExit) as error:
        run.main(['--mac-demo-live', '--registry-dir', str(tmp_path / 'run'), '--submission-limit', '2'])
    assert error.value.code == 2 and not (tmp_path / 'run').exists()
    assert 'фиксированный лимит 1' in capsys.readouterr().err


def test_vm_guard_stays_closed_on_actual_mac(monkeypatch):
    if platform.system() != 'Darwin':
        pytest.skip('Actual Mac runtime required')
    monkeypatch.setattr(closed.telegram_config, 'load_config', lambda *args: pytest.fail('Read before VM guard'))
    with pytest.raises(ConfigurationError, match='только в D4-Debian12'):
        closed.build_closed_delivery()


def test_mac_interrupted_jobs_and_test_jobs_never_replay(tmp_path, providers, launch):
    table, messages, assertions, _ = providers
    registry = tmp_path / 'run'
    app = launch(['--mac-demo-live', '--registry-dir', str(registry)])
    with TestClient(app, base_url='http://127.0.0.1'):
        accepted = app.state.store.save(str(uuid.uuid4()), 'synthetic', Lead.model_validate(fields()), [picture('JPEG')])
        with app.state.store.connection() as connection:
            connection.execute("UPDATE deliveries SET status='live_processing',attempts=1,error=? WHERE channel='telegram'",
                               (json.dumps({'linked': True}),))
    reopened = launch(['--registry-dir', str(registry)])
    with TestClient(reopened, base_url='http://127.0.0.1') as client:
        assert states(get_receipt(client, accepted).json()) == {'telegram': 'live_unknown', 'sheets': 'live_failed'}
        legacy = reopened.state.store.save(str(uuid.uuid4()), 'synthetic', Lead.model_validate(fields()),
                                      [picture('JPEG')], test_delivery=True)
        for send_one, adapter in zip((send_telegram, send_sheet), closed.build_mac_demo_delivery()):
            with pytest.raises(Failure) as rejected:
                send_one(reopened.state.store, legacy['submission_id'], adapter, linked_live=True)
            assert rejected.value.status == 409
    assert not messages and not assertions and not table.calls


@pytest.mark.parametrize('fault', ['missing_config', 'disabled', 'bad_key', 'key_symlink'])
def test_incomplete_mac_configuration_never_starts(tmp_path, mac_files, launch, fault, capsys):
    if fault == 'missing_config':
        (mac_files / '.env.sheets').unlink()
    if fault == 'disabled':
        private_file(mac_files / '.env', config_text(False))
    if fault == 'bad_key':
        path = mac_files / '.secrets/sheets-service-account.json'
        info = json.loads(path.read_text())
        info['private_key'] = '-----BEGIN PRIVATE KEY-----\nsynthetic-invalid-key\n-----END PRIVATE KEY-----'
        private_file(path, json.dumps(info))
    if fault == 'key_symlink':
        path = mac_files / '.secrets/sheets-service-account.json'
        alternate = mac_files / '.secrets/synthetic-copy'
        path.rename(alternate)
        path.symlink_to(alternate)
    with pytest.raises(SystemExit) as rejected:
        launch(['--mac-demo-live', '--registry-dir', str(tmp_path / 'run')])
    assert rejected.value.code == 2 and not (tmp_path / 'run').exists()
    captured = capsys.readouterr()
    assert TOKEN not in captured.err + captured.out and MARKER not in captured.err + captured.out


@pytest.mark.parametrize('fault', ['redirect', 'malformed', 'missing_token', 'oversized', 'encoding', 'timeout', 'signing'])
def test_native_authorization_rejects_bad_responses_without_retry(mac_files, monkeypatch, fault):
    _, adapter = closed.build_mac_demo_delivery()
    auth = adapter.authorization
    calls = []

    def route(request):
        calls.append(request)
        if fault == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://example.test/not-allowed'})
        if fault == 'malformed':
            return httpx.Response(200, content=b'{invalid')
        if fault == 'missing_token':
            return httpx.Response(200, json={'expires_in': 3600})
        if fault == 'oversized':
            return httpx.Response(200, content=b'x' * 65537)
        if fault == 'encoding':
            return httpx.Response(200, headers={'Content-Encoding': 'unsupported'}, content=b'x')
        raise httpx.ReadTimeout('Synthetic OAuth response lost')

    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(route))
    if fault == 'signing':
        def broken(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ['synthetic'], stderr=b'synthetic-private-material')
        monkeypatch.setattr(subprocess, 'run', broken)
    with pytest.raises(sheets_config.ConfigurationError) as rejected:
        auth.token()
    assert len(calls) == (0 if fault == 'signing' else 1)
    assert str(rejected.value) == 'Авторизация Mac недоступна. Значения скрыты.'
    assert not any(secret in repr(auth) for secret in (ACCOUNT, 'PRIVATE KEY', 'synthetic-mac-key'))


@pytest.mark.parametrize('repeat', [False, True])
def test_parallel_mac_requests_respect_one_submission(tmp_path, providers, launch, repeat):
    table, messages, assertions, _ = providers
    keys = [str(uuid.uuid4()), str(uuid.uuid4())]
    if repeat:
        keys[1] = keys[0]
    app = launch(['--mac-demo-live', '--registry-dir', str(tmp_path / 'run')])
    with TestClient(app, base_url='http://127.0.0.1') as client, ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda key: send(client, key, headers=MAC_HEADER), keys))
        assert sorted(r.status_code for r in responses) == ([200, 201] if repeat else [201, 409])
        accepted = next(r.json() for r in responses if r.status_code == 201)
        assert set(states(get_receipt(client, accepted).json()).values()) == {'live_delivered'}
        if repeat:
            assert responses[0].json()['submission_id'] == responses[1].json()['submission_id']
        store = app.state.store
        later = store.settings.clock() + store.settings.retention + 1
        store.settings.clock = lambda: later
        assert store.cleanup(apply=True) == 1
        assert send(client, headers=MAC_HEADER).status_code == 409
    assert len(messages) == len(assertions) == table.append_count == 1
