"""Форма, два задания SQLite и настоящий Google SDK поверх подставных транспортов."""
from email import policy
from email.parser import BytesParser
import json
import socket
import sys
from urllib.parse import parse_qs
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from server import manage
from server.app import create_app
from server.model import Settings
from server.sheets import SheetsAdapter, row_for_submission
from server.sheets_auth import SDKAuthorization
from server.sheets_config import SCOPE, TOKEN_URI
from server.store import Store
from server.telegram import TelegramAdapter
from test_server import fields, get_receipt, picture, send
from test_sheets import MARKER, Table, cfg, error
from test_telegram import config as telegram_config, response as telegram_response


@pytest.fixture(scope='module')
def sdk_key():
    pytest.importorskip('google.oauth2.service_account')
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode('ascii')
    public_key = key.public_key().public_bytes(serialization.Encoding.PEM,
                                              serialization.PublicFormat.SubjectPublicKeyInfo)
    return pem, public_key


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('External sockets forbidden')
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(socket.socket, 'connect', deny)


def prepare_channels(settings, sdk_key, table, *, telegram_status=200, oauth_status=200):
    from google.auth import jwt

    sheets_config = cfg()
    telegram_calls = []
    oauth_calls = []

    def oauth(request):
        assert str(request.url) == TOKEN_URI and request.method == 'POST'
        oauth_calls.append(request.method)
        data = parse_qs(request.content.decode('ascii'))
        assert data['grant_type'] == ['urn:ietf:params:oauth:grant-type:jwt-bearer']
        claims = jwt.decode(data['assertion'][0], {'d4-linked': sdk_key[1]}, audience=TOKEN_URI)
        assert claims['iss'] == sheets_config.service_account and claims['scope'] == SCOPE
        return httpx.Response(oauth_status, json={'access_token': MARKER, 'expires_in': 3600})

    def telegram(request):
        parts = BytesParser(policy=policy.default).parsebytes(
            b'MIME-Version: 1.0\r\nContent-Type: ' + request.headers['content-type'].encode('ascii')
            + b'\r\n\r\n' + request.content
        )
        form = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
                for part in parts.iter_parts()}
        caption = form['caption'].decode('utf-8')
        sid = caption.splitlines()[0].split()[-1]
        telegram_calls.append(sid)
        assert 'demo@example.test' not in caption and 'Тестовый след чая' not in caption
        assert all(text in caption for text in ('Прямой', 'Два места', 'Ткань / рогожка'))
        with Store(settings).connection() as connection:
            photo = connection.execute('SELECT content FROM photos WHERE submission_id=?', (sid,)).fetchone()
        assert photo and form['photo'] == photo[0] and photo[0].startswith(b'\xff\xd8')
        return telegram_response(telegram_status)

    authorization = SDKAuthorization(sheets_config, {
        'type': 'service_account', 'client_email': sheets_config.service_account,
        'token_uri': TOKEN_URI, 'private_key_id': 'd4-linked', 'private_key': sdk_key[0],
    }, transport=httpx.MockTransport(oauth))
    return (
        TelegramAdapter(telegram_config(), transport=httpx.MockTransport(telegram)),
        SheetsAdapter(sheets_config, authorization, transport=httpx.MockTransport(table)),
    ), telegram_calls, oauth_calls


def states(receipt):
    return {job['channel']: job['status'] for job in receipt['channels']}


def channel_message(receipt, channel):
    return next(job['message'] for job in receipt['channels'] if job['channel'] == channel)


def write_examples(tmp_path, **receipts):
    (tmp_path / 'receipt-examples.json').write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2), encoding='utf-8',
    )


def test_two_new_forms_send_once_and_replay_keeps_number(tmp_path, sdk_key):
    settings = Settings(data_dir=tmp_path / 'registry', minute_limit=6)
    table = Table()
    with TestClient(create_app(settings), base_url='http://127.0.0.1') as client:
        old = send(client).json()
        assert set(states(old).values()) == {'not_connected'}
    adapters, messages, oauth = prepare_channels(settings, sdk_key, table)
    app = create_app(settings, test_delivery=adapters)
    photo = [('photos', ('original.png', picture(exif=True), 'image/png'))]
    keys = [str(uuid.uuid4()), str(uuid.uuid4())]
    with TestClient(app, base_url='http://127.0.0.1') as client:
        submissions = []
        for key in keys:
            posted = send(client, key, photos=photo)
            assert posted.status_code == 201
            accepted = posted.json()
            assert set(states(accepted).values()) == {'mock_queued'}
            submissions.append(accepted)
        receipts = [get_receipt(client, accepted).json() for accepted in submissions]
        assert all(set(states(receipt).values()) == {'mock_delivered'} for receipt in receipts)
        assert all(channel_message(receipt, 'sheets') == 'Запись подтверждена сверкой всех девяти полей.'
                   for receipt in receipts)
        assert set(states(get_receipt(client, old).json()).values()) == {'not_connected'}
        for key, accepted in zip(keys, submissions):
            replay = send(client, key, photos=photo)
            assert replay.status_code == 200 and replay.json()['replayed']
            assert replay.json()['submission_id'] == accepted['submission_id']
    ids = [item['submission_id'] for item in submissions]
    assert messages == ids and oauth == ['POST', 'POST']
    assert table.append_count == 2 and [row[0] for row in table.rows] == ids
    assert table.calls == ['header', 'scan', 'append', 'readback'] * 2
    with app.state.store.connection() as connection:
        expected = [row_for_submission(app.state.store, sid, connection) for sid in ids]
    assert table.rows == expected
    assert all(len(row) == 9 and row[7] == 'demo@example.test' for row in table.rows)
    with TestClient(create_app(settings, test_delivery=adapters), base_url='http://127.0.0.1') as client:
        replay = send(client, keys[0], photos=photo)
        assert replay.status_code == 200 and replay.json()['replayed']
        assert replay.json()['submission_id'] == ids[0]
        assert states(get_receipt(client, submissions[0]).json()) == states(receipts[0])
    assert messages == ids and table.append_count == 2 and len(oauth) == 2
    pending_example = {key: submissions[0][key] for key in ('submission_id', 'channels')}
    write_examples(tmp_path, waiting=pending_example, first=receipts[0], second=receipts[1])


@pytest.mark.parametrize(('fault', 'telegram_state', 'sheet_state', 'writes'), [
    ('telegram_forbidden', 'mock_failed', 'mock_delivered', 1),
    ('sheets_forbidden', 'mock_delivered', 'mock_failed', 0),
    ('telegram_rate_limit', 'mock_failed', 'mock_delivered', 1),
    ('sheets_rate_limit', 'mock_delivered', 'mock_failed', 0),
    ('oauth_rate_limit', 'mock_delivered', 'mock_failed', 0),
    ('timeout_after_write', 'mock_delivered', 'mock_unknown', 1),
    ('readback_mismatch', 'mock_delivered', 'mock_unknown', 1),
])
def test_independent_outcomes_survive_restart(tmp_path, sdk_key, fault, telegram_state, sheet_state, writes):
    settings = Settings(data_dir=tmp_path / 'registry')
    table = Table()
    if fault == 'sheets_forbidden':
        table.faults['header'] = error(403)
    if fault == 'sheets_rate_limit':
        table.faults['header'] = error(429, retry=45)
    if fault == 'timeout_after_write':
        def timeout_after_write(request, sheet):
            assert len(sheet.rows) == 1
            raise httpx.ReadTimeout('Synthetic response lost after append')
        table.faults['append'] = timeout_after_write
    if fault == 'readback_mismatch':
        def changed_field(request, sheet):
            row = sheet.rows[-1][:-1] + ['2']
            return httpx.Response(200, json={'range': 'Leads!A2:I2', 'majorDimension': 'ROWS', 'values': [row]})
        table.faults['readback'] = changed_field
    telegram_status = {'telegram_forbidden': 403, 'telegram_rate_limit': 429}.get(fault, 200)
    adapters, messages, oauth = prepare_channels(
        settings, sdk_key, table, telegram_status=telegram_status,
        oauth_status=429 if fault == 'oauth_rate_limit' else 200,
    )
    key = str(uuid.uuid4())
    with TestClient(create_app(settings, test_delivery=adapters), base_url='http://127.0.0.1') as client:
        result = send(client, key)
        assert result.status_code == 201
        accepted = result.json()
        receipt = get_receipt(client, accepted).json()
        assert states(receipt) == {'telegram': telegram_state, 'sheets': sheet_state}
    with TestClient(create_app(settings, test_delivery=adapters), base_url='http://127.0.0.1') as client:
        assert send(client, key).json()['submission_id'] == accepted['submission_id']
        assert get_receipt(client, accepted).json() == receipt
    assert messages == [accepted['submission_id']] and oauth == ['POST']
    assert table.append_count == writes
    if fault == 'oauth_rate_limit':
        assert table.calls == []
    if fault in {'telegram_rate_limit', 'sheets_rate_limit'}:
        channel = fault.removesuffix('_rate_limit')
        assert channel_message(receipt, channel) == 'Доставка не состоялась. Повтора не будет.'
        assert table.calls == (['header'] if channel == 'sheets' else ['header', 'scan', 'append', 'readback'])
    write_examples(tmp_path, **{fault: receipt})


def test_restart_settles_interrupted_test_jobs_without_replay(tmp_path, sdk_key):
    settings = Settings(data_dir=tmp_path / 'registry')
    table = Table()
    adapters, messages, oauth = prepare_channels(settings, sdk_key, table)
    store = Store(settings)
    store.initialize()
    from server.model import Lead
    from server.store import fingerprint

    key = str(uuid.uuid4())
    lead = Lead.model_validate(fields())
    original = picture()
    accepted = store.save(key, fingerprint(lead, [(original, 'image/png')]), lead,
                          [picture('JPEG')], test_delivery=True)
    with store.connection() as connection:
        connection.execute("UPDATE deliveries SET status='mock_processing' WHERE channel='sheets'")
    with TestClient(create_app(settings, test_delivery=adapters), base_url='http://127.0.0.1') as client:
        assert send(client, key).json()['submission_id'] == accepted['submission_id']
        receipt = get_receipt(client, accepted).json()
    assert states(receipt) == {'telegram': 'mock_failed', 'sheets': 'mock_unknown'}
    assert not messages and not oauth and not table.calls
    write_examples(tmp_path, interrupted=receipt)


def test_legacy_sheets_simulate_receipt_reports_imitation(tmp_path, monkeypatch, capsys):
    settings = Settings(data_dir=tmp_path / 'registry')
    with TestClient(create_app(settings), base_url='http://127.0.0.1') as client:
        accepted = send(client).json()
    monkeypatch.setattr(manage, 'Settings', lambda: settings)
    monkeypatch.setattr(sys, 'argv', ['manage', 'simulate', '--channel', 'sheets', '--scenario', 'success'])
    manage.main()
    assert 'Локальная имитация: delivered' in capsys.readouterr().out
    with TestClient(create_app(settings), base_url='http://127.0.0.1') as client:
        receipt = get_receipt(client, accepted).json()
    assert states(receipt)['sheets'] == 'delivered'
    assert channel_message(receipt, 'sheets') == 'Успех записи сымитирован. Чтение и сверка строки не выполнялись.'
    write_examples(tmp_path, legacy_sheets=receipt)
