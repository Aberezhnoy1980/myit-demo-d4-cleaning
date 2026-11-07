"""B1: только искусственная конфигурация, временные реестры и MockTransport."""
from io import BytesIO
from pathlib import Path
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

import httpx
from PIL import Image
import pytest

from server.app import create_app
from fastapi.testclient import TestClient
from server.delivery import TestAdapter, arm_test_jobs, step
from server.model import Failure, Lead, ROOT, Settings
from server.store import Store, SCHEMA
from server.telegram import PhotoRequest, TelegramAdapter, send_one
from server.telegram_config import ConfigurationError, TelegramConfig, load_config, parse_config

TOKEN = '123456789:D4_SYNTHETIC_UNIQUE_SECRET_MARKER_XXXXXXXXXX'
CHAT = '-1001234567890'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('External network forbidden in Telegram tests')
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)


def config(enabled=False, chat=CHAT):
    return TelegramConfig(TOKEN, chat, enabled)


def config_text(enabled=False):
    return f'D4_TELEGRAM_TOKEN={TOKEN}\nD4_TELEGRAM_CHAT_ID={CHAT}\nD4_TELEGRAM_LIVE={int(enabled)}\n'


def jpeg():
    file = BytesIO(); Image.new('RGB', (12, 12), 'white').save(file, 'JPEG')
    return file.getvalue()


@pytest.fixture
def registry(tmp_path):
    clock = [time.time()]
    store = Store(Settings(data_dir=tmp_path / 'data', clock=lambda: clock[0]))
    store.initialize()
    lead = Lead(sofa='Прямой', size='Два места', material='Ткань / рогожка',
                description='Синтетический тест B1.', channel='email', contact='demo@example.test')
    accepted = store.save(str(uuid.uuid4()), 'synthetic-body', lead, [jpeg()])
    return store, accepted, clock


def response(status=200, *, chat=CHAT, **kwargs):
    if status == 200:
        data = {'ok': True, 'result': {'message_id': 17, 'chat': {'id': int(chat)}, 'photo': [{'file_id': 'synthetic-file','width':12,'height':12}]}}
    else:
        data = {'ok': False, 'error_code': status, 'description': TOKEN}
        if status == 429:
            data['parameters'] = {'retry_after': kwargs.get('retry_after', 45)}
    return httpx.Response(status, json=data)


def mock(handler, cfg=None):
    return TelegramAdapter(cfg or config(), transport=httpx.MockTransport(handler))


def live_model(monkeypatch, handler, cfg=None):
    # Проверка ветки live на синтетических данных. Сеть всё равно MockTransport.
    monkeypatch.setattr(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(handler))
    return TelegramAdapter(cfg or config(True), allow_live=True)


def current_job(store):
    with store.connection() as c:
        return dict(c.execute("SELECT * FROM deliveries WHERE channel='telegram'").fetchone())


def preserved_rows(store):
    with store.connection() as c:
        rows = [tuple(r) for r in c.execute('SELECT * FROM submissions ORDER BY id')]
        rows += [tuple(r) for r in c.execute('SELECT * FROM photos ORDER BY name')]
        rows += [tuple(r) for r in c.execute("SELECT * FROM deliveries WHERE channel='sheets' ORDER BY submission_id")]
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def test_blank_template_is_disabled_and_only_env_example_is_shareable():
    assert not parse_config((ROOT / '.env.example').read_text()).enabled
    patterns = (ROOT / '.gitignore').read_text().splitlines()
    def ignored(name):
        value = False
        for pattern in patterns:
            if pattern and not pattern.startswith('#'):
                negate = pattern.startswith('!')
                if fnmatch.fnmatchcase(name, pattern.lstrip('!')):
                    value = not negate
        return value
    assert ignored('.env') and ignored('.env.production') and ignored('.env.backup')
    assert not ignored('.env.example')


@pytest.mark.parametrize('bad', [
    'export D4_TELEGRAM_TOKEN=anything', 'WEBHOOK=https://example.test',
    'D4_TELEGRAM_LIVE=1\nD4_TELEGRAM_LIVE=0', 'D4_TELEGRAM_LIVE=true',
    'D4_TELEGRAM_TOKEN=$(touch /tmp/d4-never-run)', 'D4_TELEGRAM_TOKEN=`id`',
    config_text().replace(CHAT, '@public_group'), config_text().replace(CHAT, '12345'),
    config_text().replace(CHAT, '-999999999999999999'),
    config_text().replace(TOKEN, TOKEN + '/sendMessage?x=1'),
    config_text().replace(TOKEN, '"' + TOKEN + '"'), 'x' * 4097,
], ids=['export','unknown-key','duplicate','flag','dollar','backtick','named-chat','positive-chat','large-chat','token-url','quoted-token','large-file'])
def test_config_rejects_shell_urls_unknown_and_duplicates(bad):
    with pytest.raises(ConfigurationError) as error:
        parse_config(bad)
    assert TOKEN not in str(error.value)


def test_config_repr_hides_token_and_recipient():
    cfg = parse_config('# synthetic\n' + config_text())
    assert cfg.chat_id == CHAT and cfg.token == TOKEN and not cfg.enabled
    assert TOKEN not in repr(cfg) and CHAT not in repr(cfg)


def test_private_file_loader_reads_only_explicit_synthetic_file(tmp_path, monkeypatch):
    path = tmp_path / '.env'; path.write_text(config_text()); path.chmod(0o600)
    assert load_config(path) == config()
    path.chmod(0o400); assert load_config(path) == config()
    path.chmod(0o644)
    with pytest.raises(ConfigurationError): load_config(path)
    link = tmp_path / 'link'; link.symlink_to(path)
    with pytest.raises(ConfigurationError): load_config(link)
    monkeypatch.setenv('D4_TELEGRAM_TOKEN', TOKEN)
    with pytest.raises(ConfigurationError): load_config(tmp_path / 'missing.env')


def test_default_adapter_never_constructs_transport(monkeypatch):
    def deny(**kwargs): raise AssertionError('Transport must remain unused')
    monkeypatch.setattr(httpx, 'HTTPTransport', deny)
    with pytest.raises(ConfigurationError): TelegramAdapter(config())
    with pytest.raises(ConfigurationError): TelegramAdapter(config(True))
    with pytest.raises(ConfigurationError): TelegramAdapter(TelegramConfig(), allow_live=True)
    with pytest.raises(ConfigurationError): TelegramAdapter(config(), transport=object())


def test_sendphoto_contract_and_mock_receipt(registry):
    store, accepted, _ = registry; before = preserved_rows(store); calls = []
    def handler(request):
        calls.append(1)
        assert request.method == 'POST' and request.url.scheme == 'https'
        assert request.url.host == 'api.telegram.org' and request.url.path == '/bot' + TOKEN + '/sendPhoto'
        assert not request.url.query and not request.url.username and not request.url.password
        body = request.content
        assert CHAT.encode() in body and accepted['submission_id'].encode() in body
        assert b'filename="d4-demo.jpg"' in body and b'Content-Type: image/jpeg' in body
        assert b'demo@example.test' not in body and accepted['receipt_key'].encode() not in body and TOKEN.encode() not in body
        assert request.extensions['timeout'] == {'connect': 3, 'read': 5, 'write': 5, 'pool': 1}
        return response()
    outcome = send_one(store, accepted['submission_id'], mock(handler))
    assert outcome.kind == 'delivered' and calls == [1]
    assert current_job(store)['status'] == 'mock_delivered' and current_job(store)['simulated'] == 1
    receipt = store.receipt(accepted['submission_id'], accepted['receipt_key'])
    assert 'доставка не подключена' in receipt['message']
    assert next(j for j in receipt['channels'] if j['channel'] == 'telegram')['mode'] == 'test'
    assert 'message_id' not in json.dumps(receipt) and TOKEN not in json.dumps(receipt)
    assert preserved_rows(store) == before


@pytest.mark.parametrize('status,expected', [(200, 'delivered'), (400, 'failed'), (401, 'failed'), (403, 'failed'), (404, 'failed'), (429, 'pending'), (500, 'unknown')])
def test_live_state_model_offline_and_sheets_independence(registry, monkeypatch, status, expected, caplog):
    store, accepted, _ = registry; before = preserved_rows(store)
    with caplog.at_level(logging.DEBUG):
        outcome = send_one(store, accepted['submission_id'], live_model(monkeypatch, lambda request: response(status)))
    assert outcome.kind == expected
    assert current_job(store)['status'] == 'live_' + expected
    assert current_job(store)['simulated'] == 0
    receipt = store.receipt(accepted['submission_id'], accepted['receipt_key'])
    assert next(j for j in receipt['channels'] if j['channel'] == 'telegram')['mode'] == 'live'
    assert 'прочитал' not in receipt['message'] and 'Google Sheets не подключены' in receipt['message']
    if expected == 'delivered':
        assert 'создание сообщения' in receipt['message'] and 'прочтение человеком не подтверждено' in receipt['message']
    assert preserved_rows(store) == before
    assert TOKEN not in caplog.text + repr(outcome) + json.dumps(receipt) + current_job(store)['error']


@pytest.mark.parametrize('telegram_state', ['live_delivered', 'live_unknown', 'test_only'])
def test_render_receipt_scopes_simulation_to_each_channel(registry, monkeypatch, telegram_state):
    store, accepted, _ = registry
    if telegram_state != 'test_only':
        def handler(request):
            if telegram_state == 'live_unknown':
                raise httpx.ReadTimeout('Synthetic uncertain response')
            return response()
        send_one(store, accepted['submission_id'], live_model(monkeypatch, handler))
        store.initialize()
        telegram_before = current_job(store)
    arm_test_jobs(store)
    if telegram_state == 'test_only':
        assert step(store, 'telegram', TestAdapter('success')) == 'delivered'
    assert step(store, 'sheets', TestAdapter('success')) == 'delivered'
    if telegram_state != 'test_only':
        assert current_job(store) == telegram_before
    receipt = store.receipt(accepted['submission_id'], accepted['receipt_key'])
    channels = {channel['channel']: channel for channel in receipt['channels']}
    assert channels['sheets']['simulated'] == 1
    assert channels['telegram']['status'] == ('delivered' if telegram_state == 'test_only' else telegram_state)

    # Фактический renderReceipt, исполняемый Node VM. Только DOM-модель, без браузера/сети.
    script = r'''
const fs = require('fs'); const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const fn = source.slice(source.indexOf('function renderReceipt(data)'), source.indexOf("document.querySelector('#refresh-receipt')"));
const make = () => ({textContent:'', hidden:false, children:[], append(...v){this.children.push(...v)}, replaceChildren(){this.children=[]}, focus(){}});
const nodes = Object.fromEntries(['#result-title','#receipt-details'].map(id=>[id,make()]));
const data = JSON.parse(fs.readFileSync(0,'utf8'));
const context = {document:{querySelector:id=>nodes[id],createElement:make},summary:make(),result:make(),data};
vm.createContext(context);vm.runInContext(fn+';renderReceipt(data);',context);
process.stdout.write(JSON.stringify({title:nodes['#result-title'].textContent,notes:nodes['#receipt-details'].children.map(n=>n.textContent)}));
'''
    rendered = subprocess.run(['node', '-e', script, str(ROOT / 'public/app.js')],
                              input=json.dumps(receipt), text=True, capture_output=True, check=True, timeout=10)
    ui = json.loads(rendered.stdout)
    assert ui['title'] == receipt['message']
    simulated_notes = [note for note in ui['notes'] if 'тестовый режим' in note]
    sheets_note = 'Google Sheets: тестовый режим. Реальная отправка по этому каналу не выполнялась.'
    telegram_note = 'Telegram: тестовый режим. Реальная отправка по этому каналу не выполнялась.'
    assert set(simulated_notes) == ({sheets_note, telegram_note} if telegram_state == 'test_only' else {sheets_note})
    assert 'Реальных отправок нет' not in '\n'.join(ui['notes'])
    if telegram_state == 'live_delivered':
        assert 'создание сообщения' in ui['title'] and 'прочтение человеком не подтверждено' in ui['title']
    elif telegram_state == 'live_unknown':
        assert 'неизвестен' in ui['title'] and 'не повторяйте' in ui['title']
    else:
        assert 'доставка не подключена' in ui['title']


@pytest.mark.parametrize('error,kind', [(httpx.ConnectError,'pending'),(httpx.ConnectTimeout,'pending'),(httpx.PoolTimeout,'pending'),
    (httpx.ReadTimeout,'unknown'),(httpx.WriteTimeout,'unknown'),(httpx.ReadError,'unknown'),(httpx.WriteError,'unknown'),
    (httpx.RemoteProtocolError,'unknown'),(RuntimeError,'unknown')])
def test_network_failure_classification_and_secret_exclusion(registry, error, kind, caplog, capsys):
    store, accepted, _ = registry
    def handler(request):
        logging.getLogger('httpx').warning('%s %s', request.url, TOKEN)
        raise error(TOKEN + ' ' + str(request.url))
    adapter = mock(handler)
    previous = logging.root.manager.disable
    with caplog.at_level(logging.DEBUG):
        outcome = send_one(store, accepted['submission_id'], adapter)
        logging.getLogger('d4-test-after').warning('logging restored')
    captured = capsys.readouterr()
    text = caplog.text + captured.out + captured.err + repr(outcome) + repr(adapter) + repr(adapter.config)
    assert TOKEN not in text and 'D4_SYNTHETIC_UNIQUE_SECRET_MARKER' not in text and CHAT not in text
    assert logging.root.manager.disable == previous and 'logging restored' in caplog.text
    assert outcome.kind == kind


@pytest.mark.parametrize('case', ['broken','array','partial_success','malformed_photo','wrong_recipient','redirect','large','encoding','truncated_stream'])
def test_unconfirmed_response_is_unknown_without_leaking(registry, case, caplog):
    store, accepted, _ = registry; calls=[]
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"ok":true,'
            raise httpx.ReadError(TOKEN)
    def handler(request):
        calls.append(str(request.url.host))
        if case=='broken': return httpx.Response(200, content=b'{' + TOKEN.encode())
        if case=='array': return httpx.Response(200, json=[TOKEN])
        if case=='partial_success': return httpx.Response(200, json={'ok':True,'description':TOKEN})
        if case=='malformed_photo': return httpx.Response(200,json={'ok':True,'result':{'message_id':17,'chat':{'id':int(CHAT)},'photo':[None]}})
        if case=='wrong_recipient': return response(chat='-1001234567891')
        if case=='redirect': return httpx.Response(302, headers={'Location':'https://example.test/'+TOKEN})
        if case=='large': return httpx.Response(200, content=TOKEN.encode()*3000)
        if case=='encoding': return httpx.Response(200, stream=httpx.ByteStream(b'x'), headers={'Content-Encoding':'gzip'})
        return httpx.Response(200, stream=Broken())
    with caplog.at_level(logging.DEBUG):
        outcome = send_one(store, accepted['submission_id'], mock(handler))
    assert outcome.kind=='unknown' and calls==['api.telegram.org']
    assert TOKEN not in caplog.text + repr(outcome) and TOKEN not in current_job(store)['error']
    with pytest.raises(Failure): send_one(store, accepted['submission_id'], mock(handler))
    assert len(calls)==1


def test_429_manual_delay_max_three_and_recipient_binding(registry):
    store, accepted, clock=registry; calls=[]
    def handler(request): calls.append(1);return response(429,retry_after=60)
    adapter=mock(handler)
    assert send_one(store,accepted['submission_id'],adapter).kind=='pending'
    with pytest.raises(Failure) as too_soon: send_one(store,accepted['submission_id'],adapter)
    assert too_soon.value.status==429 and len(calls)==1
    clock[0]+=61
    with pytest.raises(Failure) as changed: send_one(store,accepted['submission_id'],mock(handler,config(chat='-1001234567891')))
    assert changed.value.status==409 and len(calls)==1
    assert send_one(store,accepted['submission_id'],adapter).kind=='pending'
    clock[0]+=61
    assert send_one(store,accepted['submission_id'],adapter).kind=='failed'
    with pytest.raises(Failure): send_one(store,accepted['submission_id'],adapter)
    assert current_job(store)['attempts']==3 and len(calls)==3


@pytest.mark.parametrize('retry', [None,0,-1,True,1.5,3601,TOKEN], ids=['missing','zero','negative','bool','float','too-long','text'])
def test_invalid_retry_after_is_terminal_unknown(registry,retry):
    store,accepted,_=registry
    assert send_one(store,accepted['submission_id'],mock(lambda request:response(429,retry_after=retry))).kind=='unknown'


def test_repeat_success_no_second_request_and_replay_receipt(registry,monkeypatch):
    store,accepted,_=registry;calls=[]
    def handler(request):calls.append(1);return response()
    adapter=live_model(monkeypatch,handler)
    assert send_one(store,accepted['submission_id'],adapter).kind=='delivered'
    assert send_one(store,accepted['submission_id'],adapter).reason=='already_created'
    assert len(calls)==1
    with store.connection() as c:
        key=c.execute('SELECT request_key FROM submissions').fetchone()[0]
    replay=store.replay(key,'synthetic-body')
    assert 'создание сообщения' in replay['message'] and replay['replayed']


def test_expired_and_invalid_id_no_claim(registry):
    store,accepted,clock=registry; before=current_job(store)
    def denied(request):raise AssertionError('No request allowed')
    adapter=mock(denied)
    with pytest.raises(Failure):send_one(store,'not-a-uuid',adapter)
    clock[0]+=store.settings.retention
    with pytest.raises(Failure) as expired:send_one(store,accepted['submission_id'],adapter)
    assert expired.value.status==410 and current_job(store)==before


def test_old_simulated_never_becomes_live_and_mock_never_becomes_live(registry,monkeypatch):
    store,accepted,_=registry
    arm_test_jobs(store);before=current_job(store)
    with pytest.raises(Failure):send_one(store,accepted['submission_id'],live_model(monkeypatch,lambda r:response()))
    assert current_job(store)==before


def test_crashed_processing_recovers_unknown_without_request(registry,monkeypatch):
    store,accepted,_=registry;adapter=live_model(monkeypatch,lambda r:(_ for _ in ()).throw(AssertionError('No request')))
    with store.connection() as c:
        c.execute("UPDATE deliveries SET status='live_processing',attempts=1,error=? WHERE channel='telegram'",
                  (json.dumps({'binding':adapter.config.binding}),))
    assert send_one(store,accepted['submission_id'],adapter).kind=='unknown'
    assert current_job(store)['status']=='live_unknown'
    with pytest.raises(Failure):send_one(store,accepted['submission_id'],adapter)


def test_storage_failure_after_provider_response_blocks_repeat(registry,monkeypatch):
    store,accepted,_=registry;calls=[]
    def handler(request):
        calls.append(1)
        with store.connection() as c:
            c.execute("CREATE TRIGGER fail_outcome BEFORE UPDATE ON deliveries WHEN NEW.status='live_delivered' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        return response()
    adapter=live_model(monkeypatch,handler)
    assert send_one(store,accepted['submission_id'],adapter).kind=='unknown'
    assert current_job(store)['status']=='live_processing'
    assert send_one(store,accepted['submission_id'],adapter).kind=='unknown'
    assert len(calls)==1 and current_job(store)['status']=='live_unknown'


def test_startup_and_post_never_construct_telegram_transport(registry,monkeypatch):
    store,accepted,_=registry
    monkeypatch.setattr(httpx,'HTTPTransport',lambda **kwargs:(_ for _ in ()).throw(AssertionError('No transport')))
    with TestClient(create_app(store.settings),base_url='http://127.0.0.1') as client:
        assert client.get('/api/health').status_code==200
        assert client.get('/api/receipts/'+accepted['submission_id'],headers={'X-Receipt-Key':accepted['receipt_key']}).status_code==200
        lead={'sofa':'Прямой','size':'Два места','material':'Ткань / рогожка','description':'Тест B1','channel':'email','contact':'demo@example.test'}
        assert client.post('/api/submissions',headers={'Idempotency-Key':str(uuid.uuid4())},data=lead,
                           files=[('photos',('test.jpg',jpeg(),'image/jpeg'))]).status_code==201


def test_cli_default_does_not_read_configuration(registry,monkeypatch,capsys):
    from server import manage
    import server.telegram_config as settings_module
    monkeypatch.setattr(settings_module,'load_config',lambda p:(_ for _ in ()).throw(AssertionError('No secret read')))
    monkeypatch.setattr(sys,'argv',['manage','telegram-send','--submission-id',registry[1]['submission_id']])
    with pytest.raises(SystemExit) as stopped:manage.main()
    assert stopped.value.code==2 and 'не читались' in capsys.readouterr().err


def test_mock_job_cannot_be_replayed_as_live(registry,monkeypatch):
    store,accepted,_=registry
    send_one(store,accepted['submission_id'],mock(lambda r:response()))
    before=current_job(store)
    with pytest.raises(Failure):send_one(store,accepted['submission_id'],live_model(monkeypatch,lambda r:response()))
    assert current_job(store)==before


def test_only_selected_submission_is_claimed(registry):
    store,accepted,_=registry
    with store.connection() as c:
        payload=c.execute('SELECT payload FROM submissions').fetchone()[0]
    second=store.save(str(uuid.uuid4()),'second-synthetic',Lead.model_validate_json(payload),[jpeg()])
    send_one(store,accepted['submission_id'],mock(lambda r:response()))
    with store.connection() as c:
        other=c.execute('SELECT status,simulated,attempts FROM deliveries WHERE submission_id=? ORDER BY channel',(second['submission_id'],)).fetchall()
    assert [tuple(r) for r in other]==[('not_connected',0,0),('not_connected',0,0)]


def test_modified_photo_rejected_before_claim(registry):
    store,accepted,_=registry
    with store.connection() as c:c.execute('UPDATE photos SET content=?',(b'not a JPEG',))
    before=current_job(store)
    with pytest.raises(Failure):send_one(store,accepted['submission_id'],mock(lambda r:response()))
    assert current_job(store)==before


CLI_HARNESS = '''
import json,os,runpy,socket,sys,time
from pathlib import Path
import httpx
def deny(*args,**kwargs):raise AssertionError('External network forbidden')
socket.create_connection=deny
socket.getaddrinfo=deny
root=Path.cwd()
mode=sys.argv[1]
def handler(request):
    calls=root/'calls.json'
    count=json.loads(calls.read_text()) if calls.exists() else 0
    calls.write_text(json.dumps(count+1))
    if mode=='hold':
        (root/'entered').touch()
        deadline=time.monotonic()+8
        while not (root/'release').exists():
            if time.monotonic()>deadline:raise RuntimeError('synthetic timeout')
            time.sleep(.02)
    if mode=='network_error':raise httpx.ReadError('synthetic secret error '+str(request.url))
    return httpx.Response(200,json={'ok':True,'result':{'message_id':17,'chat':{'id':-1001234567890},'photo':[{'file_id':'synthetic','width':12,'height':12}]}})
httpx.HTTPTransport=lambda **kwargs:httpx.MockTransport(handler)
sys.argv=['server.manage','telegram-send','--submission-id',sys.argv[2],'--execute-live']
try:runpy.run_module('server.manage',run_name='__main__')
except SystemExit:raise
'''


def cli_root(tmp_path, *, configured=True):
    shutil.copytree(ROOT/'server',tmp_path/'server',ignore=shutil.ignore_patterns('__pycache__'))
    now=time.time()
    store=Store(Settings(data_dir=tmp_path/'data',clock=lambda:now));store.initialize()
    lead=Lead(sofa='Прямой',size='Два места',material='Не знаю',description='Синтетический CLI B1.',channel='email',contact='demo@example.test')
    accepted=store.save(str(uuid.uuid4()),'synthetic-cli',lead,[jpeg()])
    if configured:
        path=tmp_path/'.env';path.write_text(config_text(True));path.chmod(0o600)
    return store,accepted


def cli_command(mode,sid):return [sys.executable,'-c',CLI_HARNESS,mode,sid]


def test_cli_no_config_stops_without_transport(tmp_path):
    store,accepted=cli_root(tmp_path,configured=False)
    before=current_job(store)
    result=subprocess.run(cli_command('success',accepted['submission_id']),cwd=tmp_path,
                          env=os.environ|{'PYTHONPATH':''},capture_output=True,text=True,timeout=10)
    assert result.returncode==2 and 'Настройка Telegram недоступна' in result.stderr
    assert not (tmp_path/'calls.json').exists() and current_job(store)==before


def test_cli_failure_output_redacts_token(tmp_path):
    store,accepted=cli_root(tmp_path)
    result=subprocess.run(cli_command('network_error',accepted['submission_id']),cwd=tmp_path,
                          env=os.environ|{'PYTHONPATH':''},capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and 'неизвестен' in result.stdout
    assert TOKEN not in result.stdout+result.stderr and CHAT not in result.stdout+result.stderr
    assert current_job(store)['status']=='live_unknown'


def test_cli_parallel_senders_and_backup_lock(tmp_path):
    store,accepted=cli_root(tmp_path);before=preserved_rows(store)
    env=os.environ|{'PYTHONPATH':''}
    first=subprocess.Popen(cli_command('hold',accepted['submission_id']),cwd=tmp_path,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+8
        while not (tmp_path/'entered').exists():
            if first.poll() is not None or time.monotonic()>deadline:pytest.fail('Mock sender did not reach protected request')
            time.sleep(.02)
        second=subprocess.run(cli_command('success',accepted['submission_id']),cwd=tmp_path,env=env,capture_output=True,text=True,timeout=5)
        assert second.returncode==2
        backup=subprocess.run([sys.executable,'-m','server.manage','backup'],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=5)
        assert backup.returncode==2 and not (tmp_path/'backups').exists()
        (tmp_path/'release').touch()
        out,err=first.communicate(timeout=8)
        assert first.returncode==0 and 'создание сообщения' in out
        third=subprocess.run(cli_command('success',accepted['submission_id']),cwd=tmp_path,env=env,capture_output=True,text=True,timeout=5)
        assert third.returncode==0
        assert json.loads((tmp_path/'calls.json').read_text())==1
        assert preserved_rows(store)==before and current_job(store)['status']=='live_delivered'
        assert TOKEN not in out+err+second.stdout+second.stderr+third.stdout+third.stderr
    finally:
        (tmp_path/'release').touch()
        if first.poll() is None:first.kill();first.wait(timeout=3)
