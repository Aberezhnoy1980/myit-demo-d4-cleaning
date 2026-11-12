"""Закрытый запуск через штатные загрузчики и подставной HTTPS-транспорт."""
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
import json
import logging
import socket
import subprocess
from urllib.parse import parse_qs
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from server import closed, sheets_config
from server.model import Failure, Lead, ROOT, Settings
from server.sheets import row_for_submission, send_one as send_sheets
from server.sheets_config import SCOPE, TOKEN_URI
from server.store import Store
from server.telegram import send_one as send_telegram
from tools import run
from test_linked_delivery import sdk_key, states, write_examples
from test_server import fields, get_receipt, picture, send
from test_sheets import ACCOUNT, MARKER, SHEET_ID, Table, error
from test_telegram import CHAT, TOKEN, config_text, response

CLOSED_MODE_HEADER = {'X-D4-Delivery-Mode': 'closed_live'}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('External sockets forbidden')
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(httpx, 'HTTPTransport', deny)


def private_file(path, text):
    path.write_text(text, encoding='utf-8')
    path.chmod(0o600)


@pytest.fixture
def local_files(tmp_path, monkeypatch):
    root = tmp_path / 'private-config'
    secrets = root / '.secrets'
    secrets.mkdir(parents=True, mode=0o700)
    key = secrets / 'sheets-service-account.json'
    private_file(root / '.env', config_text(True))
    private_file(root / '.env.sheets', f'D4_SHEETS_SPREADSHEET_ID={SHEET_ID}\n'
                 f'D4_SHEETS_SERVICE_ACCOUNT={ACCOUNT}\nD4_SHEETS_KEY_FILE={key}\nD4_SHEETS_LIVE=1\n')
    monkeypatch.setattr(closed, 'ROOT', root)
    monkeypatch.setattr(sheets_config, 'KEY_FILE', key)
    monkeypatch.setattr(closed.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(closed.platform, 'freedesktop_os_release', lambda: {'ID': 'debian', 'VERSION_ID': '12'})
    monkeypatch.setattr(closed.socket, 'gethostname', lambda: 'd4-debian12')
    return root, key


@pytest.fixture
def launch(monkeypatch):
    calls = []

    def capture(app, **options):
        assert options['host'] == '127.0.0.1'
        assert options['workers'] == 1
        assert options['proxy_headers'] is False and options['access_log'] is False
        calls.append(app)

    monkeypatch.setattr(run.uvicorn, 'run', capture)

    def start(args):
        run.main(args)
        return calls[-1]

    return start


def closed_args(registry, limit=1):
    return ['--closed-live', '--registry-dir', str(registry), '--submission-limit', str(limit)]


def client_for(app):
    return TestClient(app, base_url='http://127.0.0.1')


@pytest.fixture
def providers(local_files, sdk_key, monkeypatch):
    from google.auth import jwt

    private_file(local_files[1], json.dumps({'type': 'service_account', 'client_email': ACCOUNT,
                 'token_uri': TOKEN_URI, 'private_key_id': 'd4-closed', 'private_key': sdk_key[0]}))
    table = Table()
    messages, assertions = [], []
    faults = {}

    def route(request):
        if str(request.url) == TOKEN_URI:
            assert request.method == 'POST'
            form = parse_qs(request.content.decode('ascii'))
            assert form['grant_type'] == ['urn:ietf:params:oauth:grant-type:jwt-bearer']
            assertion, = form['assertion']
            assertions.append(assertion)
            claims = jwt.decode(assertion, {'d4-closed': sdk_key[1]}, audience=TOKEN_URI)
            assert claims['iss'] == ACCOUNT and claims['scope'] == SCOPE
            logging.getLogger('httpx').warning('Synthetic OAuth assertion %s', assertion)
            return httpx.Response(200, json={'access_token': MARKER, 'expires_in': 3600})
        if request.url.host == 'api.telegram.org':
            assert request.method == 'POST' and request.url.path == '/bot' + TOKEN + '/sendPhoto'
            parts = BytesParser(policy=policy.default).parsebytes(
                b'MIME-Version: 1.0\r\nContent-Type: ' + request.headers['content-type'].encode('ascii')
                + b'\r\n\r\n' + request.content)
            form = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
                    for part in parts.iter_parts()}
            assert form['chat_id'].decode() == CHAT
            messages.append((form['caption'].decode(), form['photo']))
            logging.getLogger('httpx').warning('Synthetic Telegram URL %s', request.url)
            if faults.get('telegram') == 'timeout':
                raise httpx.ReadTimeout('Synthetic response lost after sendPhoto: ' + TOKEN)
            return response(faults.get('telegram', 200))
        return table(request)

    def transport(**options):
        assert options == {'retries': 0, 'trust_env': False}
        return httpx.MockTransport(route)

    monkeypatch.setattr(httpx, 'HTTPTransport', transport)
    return table, messages, assertions, faults


@pytest.mark.parametrize(('fault', 'telegram_state', 'sheets_state', 'writes'), [
    ('success', 'live_delivered', 'live_delivered', 1),
    ('telegram_rejected', 'live_failed', 'live_delivered', 1),
    ('sheets_rejected', 'live_delivered', 'live_failed', 0),
    ('telegram_429', 'live_failed', 'live_delivered', 1),
    ('sheets_429', 'live_delivered', 'live_failed', 0),
    ('telegram_timeout', 'live_unknown', 'live_delivered', 1),
    ('sheets_timeout_after_append', 'live_delivered', 'live_unknown', 1),
    ('sheets_readback_mismatch', 'live_delivered', 'live_unknown', 1),
])
def test_closed_form_outcomes(tmp_path, providers, launch, sdk_key, caplog, capsys,
                              fault, telegram_state, sheets_state, writes):
    table, messages, assertions, faults = providers
    if fault.startswith('telegram_'):
        faults['telegram'] = {'telegram_rejected': 403, 'telegram_429': 429, 'telegram_timeout': 'timeout'}[fault]
    if fault == 'sheets_rejected':
        table.faults['header'] = error(403)
    if fault == 'sheets_429':
        table.faults['header'] = error(429, retry=45)
    if fault == 'sheets_timeout_after_append':
        def timeout(request, sheet):
            assert len(sheet.rows) == 1
            raise httpx.ReadTimeout('Synthetic response lost after append: ' + MARKER)
        table.faults['append'] = timeout
    if fault == 'sheets_readback_mismatch':
        def different_contact(request, sheet):
            row = sheet.rows[-1].copy()
            row[7] = 'other@example.test'
            return httpx.Response(200, json={'range': 'Leads!A2:I2', 'majorDimension': 'ROWS', 'values': [row]})
        table.faults['readback'] = different_contact

    registry = tmp_path / 'run'
    app = launch(closed_args(registry))
    key = str(uuid.uuid4())
    original = [('photos', ('original.png', picture(exif=True), 'image/png'))]
    lead = fields(description='=SUM(1,2)')
    with caplog.at_level(logging.DEBUG), client_for(app) as client:
        assert client.get('/api/health').json()['delivery'] == 'closed_live'
        assert not messages and not assertions and not table.calls
        posted = send(client, key, data=lead, photos=original, headers=CLOSED_MODE_HEADER)
        assert posted.status_code == 201
        accepted = posted.json()
        assert set(states(accepted).values()) == {'live_queued'}
        receipt = get_receipt(client, accepted).json()
        assert states(receipt) == {'telegram': telegram_state, 'sheets': sheets_state}
        replay = send(client, key, data=lead, photos=original, headers=CLOSED_MODE_HEADER)
        assert replay.status_code == 200 and replay.json()['replayed']
        assert replay.json()['submission_id'] == accepted['submission_id']

    with pytest.raises(SystemExit) as rejected:
        launch(closed_args(registry))
    assert rejected.value.code == 2
    ordinary = launch(['--registry-dir', str(registry)])
    with client_for(ordinary) as client:
        assert client.get('/api/health').json()['delivery'] == 'not_connected'
        assert send(client, key, data=lead, photos=original).json()['submission_id'] == accepted['submission_id']
        assert get_receipt(client, accepted).json() == receipt

    sid = accepted['submission_id']
    (caption, photo), = messages
    assert sid in caption and all(lead[field] in caption for field in ('sofa', 'size', 'material'))
    assert lead['contact'] not in caption and lead['description'] not in caption
    with app.state.store.connection() as connection:
        assert photo == connection.execute('SELECT content FROM photos').fetchone()[0] and photo.startswith(b'\xff\xd8')
        assert table.rows == ([row_for_submission(app.state.store, sid, connection)] if writes else [])
        jobs = connection.execute('SELECT attempts,simulated FROM deliveries').fetchall()
        assert [tuple(job) for job in jobs] == [(1, 0), (1, 0)]
    assert len(assertions) == 1 and table.append_count == writes
    if sheets_state == 'live_delivered':
        assert table.calls == ['header', 'scan', 'append', 'readback']
        assert len(table.rows[0]) == 9 and table.rows[0][5:8] == ['=SUM(1,2)', 'email', 'demo@example.test']
    output = capsys.readouterr()
    visible = caplog.text + output.out + output.err + json.dumps([accepted, receipt, replay.json()])
    assert not any(value in visible for value in (TOKEN, CHAT, MARKER, ACCOUNT, SHEET_ID, sdk_key[0], *assertions))
    assert accepted['receipt_key'] not in json.dumps(receipt) + caplog.text + output.err + output.out
    examples = {fault: receipt}
    if fault == 'success':
        examples['awaiting'] = {name: accepted[name] for name in ('submission_id', 'receipt_expires', 'channels')}
    write_examples(tmp_path, **examples)


@pytest.mark.parametrize(('fault', 'reason'), [
    ('wrong_host', 'только в D4-Debian12'),
    ('missing_limit', 'Нужны --registry-dir и --submission-limit'),
    ('missing_config', 'Приватный файл Sheets недоступен'),
    ('disabled_channel', 'оба переключателя live должны быть включены'),
    ('symlink_key', 'Не удалось прочитать согласованный приватный ключ Sheets'),
])
def test_invalid_start_stops_before_registry_and_transport(tmp_path, local_files, monkeypatch, fault, reason, capsys):
    root, key = local_files
    registry = tmp_path / 'run'
    args = closed_args(registry)
    if fault == 'wrong_host':
        monkeypatch.setattr(closed.socket, 'gethostname', lambda: 'different-host')
        monkeypatch.setattr(closed.telegram_config, 'load_config', lambda *args: pytest.fail('Read before host gate'))
    if fault == 'missing_limit':
        args = args[:-2]
    if fault == 'missing_config':
        (root / '.env.sheets').unlink()
    if fault == 'disabled_channel':
        private_file(root / '.env', config_text(False))
    if fault == 'symlink_key':
        target = root / 'synthetic-key'
        private_file(target, 'unused synthetic key')
        key.symlink_to(target)
    monkeypatch.setattr(run.uvicorn, 'run', lambda *args, **kwargs: pytest.fail('Server started'))
    with pytest.raises(SystemExit) as rejected:
        run.main(args)
    assert rejected.value.code == 2 and not registry.exists()
    captured = capsys.readouterr().err
    assert reason in captured and not any(value in captured for value in (TOKEN, CHAT, MARKER))


def test_closed_quota_is_atomic_and_cleanup_does_not_restore_it(tmp_path, providers, launch):
    table, messages, assertions, _ = providers
    app = launch(closed_args(tmp_path / 'run'))
    store = app.state.store
    clock = [1_800_000_000.0]
    store.settings.clock = lambda: clock[0]
    with client_for(app) as client, ThreadPoolExecutor(2) as pool:
        keys = [str(uuid.uuid4()), str(uuid.uuid4())]
        responses = list(pool.map(lambda key: send(client, key, headers=CLOSED_MODE_HEADER), keys))
        assert sorted(result.status_code for result in responses) == [201, 409]
        assert 'Лимит новых запросов' in next(result for result in responses if result.status_code == 409).text
        winner = next(i for i, result in enumerate(responses) if result.status_code == 201)
        replay = send(client, keys[winner], headers=CLOSED_MODE_HEADER)
        assert replay.status_code == 200 and replay.json()['submission_id'] == responses[winner].json()['submission_id']
        clock[0] += store.settings.retention + 1
        assert store.cleanup(apply=True) == 1
        assert send(client, headers=CLOSED_MODE_HEADER).status_code == 409
    assert len(messages) == len(assertions) == table.append_count == 1


def test_closed_restart_settles_interrupted_jobs_without_sending(tmp_path, providers, launch):
    table, messages, assertions, _ = providers
    registry = tmp_path / 'run'
    app = launch(closed_args(registry))
    store = app.state.store
    with client_for(app):
        accepted = store.save(str(uuid.uuid4()), 'synthetic', Lead.model_validate(fields()), [picture('JPEG')])
        with store.connection() as connection:
            connection.execute("UPDATE deliveries SET status='live_processing', attempts=1, error=? WHERE channel='telegram'",
                               (json.dumps({'linked': True, 'reason': 'processing'}),))
    with pytest.raises(RuntimeError, match='новый каталог'):
        with client_for(app):
            pass
    with client_for(launch(['--registry-dir', str(registry)])) as client:
        receipt = get_receipt(client, accepted).json()
    assert states(receipt) == {'telegram': 'live_unknown', 'sheets': 'live_failed'}
    assert not messages and not assertions and not table.calls
    write_examples(tmp_path, interrupted=receipt)


def test_ordinary_launch_and_test_jobs_never_send_live(tmp_path, providers, launch, monkeypatch):
    table, messages, assertions, _ = providers
    adapters = closed.build_closed_delivery()
    registry = tmp_path / 'existing'
    store = Store(Settings(data_dir=registry))
    store.initialize()
    accepted = store.save(str(uuid.uuid4()), 'synthetic', Lead.model_validate(fields()), [picture('JPEG')], test_delivery=True)
    for send_one, adapter in zip((send_telegram, send_sheets), adapters):
        with pytest.raises(Failure) as rejected:
            send_one(store, accepted['submission_id'], adapter, linked_live=True)
        assert rejected.value.status == 409

    def deny_load():
        pytest.fail('Unexpected credential loading')
    monkeypatch.setattr(run, 'build_closed_delivery', deny_load)
    with pytest.raises(SystemExit) as rejected:
        launch(closed_args(registry))
    assert rejected.value.code == 2
    with client_for(launch(['--registry-dir', str(registry)])) as client:
        assert set(states(get_receipt(client, accepted).json()).values()) == {'mock_failed'}
        assert set(states(send(client).json()).values()) == {'not_connected'}
    assert not messages and not assertions and not table.calls


@pytest.mark.parametrize('mode', ['not_connected', 'closed_live'])
def test_post_rejects_stale_delivery_mode_before_saving(tmp_path, launch, request, mode):
    registry = tmp_path / 'run'
    live = mode == 'closed_live'
    if live:
        table, messages, assertions, _ = request.getfixturevalue('providers')
    app = launch(closed_args(registry) if live else ['--registry-dir', str(registry)])
    stale = 'not_connected' if live else 'closed_live'
    key = str(uuid.uuid4())
    with client_for(app) as client:
        assert client.get('/api/health').json()['delivery'] == mode
        early = client.post('/api/submissions', headers={'X-D4-Delivery-Mode': stale}, content=b'invalid body')
        assert early.status_code == 409 and early.json()['delivery_mode'] == mode
        rejected = send(client, key, headers={'X-D4-Delivery-Mode': stale})
        assert rejected.status_code == 409 and rejected.json() == early.json()
        if live:
            missing = send(client, key)
            assert missing.status_code == 409 and missing.json() == early.json()
            assert not messages and not assertions and not table.calls
        with app.state.store.connection() as connection:
            for name in ('submissions', 'photos', 'deliveries', 'rate_events'):
                assert connection.execute(f'SELECT count(*) FROM {name}').fetchone()[0] == 0
        posted = send(client, key, headers={'X-D4-Delivery-Mode': mode})
        assert posted.status_code == 201 and not posted.json()['replayed']
    if live:
        assert len(messages) == len(assertions) == table.append_count == 1


@pytest.mark.parametrize('mode', ['not_connected', 'closed_live'])
def test_form_explains_delivery_before_submit(tmp_path, launch, request, mode):
    registry = tmp_path / 'run'
    if mode == 'closed_live':
        request.getfixturevalue('providers')
        args = closed_args(registry)
    else:
        args = ['--registry-dir', str(registry)]
    with client_for(launch(args + ['--prefix', '/d4'])) as client:
        health = client.get('/d4/api/health').json()
        page = client.get('/d4/').text
    assert health['delivery'] == mode
    assert page.index('id="submit-note"') < page.index('type="submit" aria-describedby="submit-note" disabled')

    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const health = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    disabled: id === '.submit', hidden: true, textContent: '', value: '', handlers: {},
    querySelector: node, querySelectorAll() { return [node('.submit')]; },
    addEventListener(event, fn) { this.handlers[event] = fn; },
    setAttribute() {}, removeAttribute() {}, replaceChildren() {}, append() {}, focus() {},
  });
  return nodes.get(id);
}
const calls = [];
let responseMode = health.delivery;
const context = vm.createContext({
  document: {baseURI: 'http://127.0.0.1/d4/', querySelector: node, getElementById: id => node('#' + id),
             createElement: tag => node('new:' + tag)},
  window: {addEventListener() {}}, URL, AbortSignal, AbortController, FormData, setTimeout, clearTimeout,
  crypto: require('node:crypto').webcrypto,
  async fetch(url, options) {
    if (options.method !== 'POST') {
      calls.push([url.pathname, options.cache]);
      return {ok: true, json: async () => health};
    }
    const mode = options.headers['X-D4-Delivery-Mode'];
    calls.push([url.pathname, mode, options.headers['Idempotency-Key']]);
    if (mode !== responseMode) {
      return {ok: false, status: 409, json: async () => ({delivery_mode: responseMode, message: 'Режим изменился.'})};
    }
    return {ok: true, status: 201, json: async () => ({submission_id: 'synthetic', receipt_expires: 1800000000})};
  },
});
function notice() {
  return ['#submit-note', '#hero-delivery-note', '#aside-delivery-note'].map(id => node(id).textContent);
}
(async () => {
  const ready = vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
  const disabledBefore = node('.submit').disabled;
  await ready;
  const initial = notice();
  const enabled = !node('.submit').disabled;
  for (const id of ['sofa', 'size', 'material', 'description', 'channel']) node('#' + id).value = 'demo';
  node('#contact').value = 'demo@example.test';
  vm.runInContext('selected = ["test-photo"]', context);
  const click = () => node('#lead-form').handlers.submit({preventDefault() {}});
  health.delivery = health.delivery === 'closed_live' ? 'not_connected' : 'closed_live';
  await click();
  const changed = notice();
  await click();
  const afterConflict = notice();
  const requestsAfterConflict = calls.length;
  const resultHidden = node('#result').hidden;
  health.delivery = responseMode;
  await click();
  process.stdout.write(JSON.stringify({disabledBefore, enabled, initial, changed, afterConflict,
    requestsAfterConflict, resultHidden, receiptShown: !node('#result').hidden, calls}));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run(['node', '-e', script, str(ROOT / 'public/app.js')], input=json.dumps(health),
                            text=True, capture_output=True, check=True, timeout=10)
    ui = json.loads(result.stdout)
    assert ui['disabledBefore'] and ui['enabled']
    stale = 'not_connected' if mode == 'closed_live' else 'closed_live'
    posts = [call for call in ui['calls'] if call[0] == '/d4/api/submissions']
    assert ui['calls'] == [['/d4/api/health', 'no-store']] * 3 + [posts[0], ['/d4/api/health', 'no-store'], posts[1]]
    assert [call[1] for call in posts] == [stale, mode]
    assert posts[0][2] == posts[1][2]
    assert ui['afterConflict'] == ui['initial'] and ui['requestsAfterConflict'] == 4
    assert ui['resultHidden'] and ui['receiptShown']
    ordinary, live = (ui['initial'], ui['changed']) if mode == 'not_connected' else (ui['changed'], ui['initial'])
    assert ordinary == ['Демо: сохранение только на этом стенде. Уведомления не отправляются автоматически.',
                        'Демо: только тестовые данные. Сохранение локально, без внешних отправок.',
                        'Внешняя доставка не подключена.']
    assert 'обработанное фото, номер заявки и параметры дивана уйдут в тестовый Telegram' in live[0]
    assert 'Данные заявки с синтетическим контактом уйдут в тестовую Google-таблицу' in live[0]
    assert all('тестовый Telegram и тестовую Google-таблицу' in note for note in live[1:])
