"""B2: временные реестры, явная искусственная авторизация, MockTransport, запрет сокетов."""
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
import base64
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
from urllib.parse import unquote
import uuid

import httpx
from PIL import Image
import pytest

from server.delivery import TestAdapter, arm_test_jobs, step
from server.model import Failure, Lead, ROOT, Settings
from server.sheets import HEADERS, OfflineAuthorization, SheetsAdapter, row_for_submission, send_one
from server.sheets_config import (ConfigurationError, KEY_FILE, MAX_ROW, SheetsConfig, load_config,
                                  parse_config, require_live_sdk, validate_service_account)
from server.store import Store

MARKER = 'SHEETS_SYNTHETIC_UNIQUE_SECRET_XXXXXXXX'
SHEET_ID = 'synthetic_spreadsheet_0123456789'
ACCOUNT = 'd4-test@synthetic-project.iam.gserviceaccount.com'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('External sockets forbidden')
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket.socket, 'connect', deny)


def cfg(**changes):
    return SheetsConfig(**({'spreadsheet_id': SHEET_ID, 'service_account': ACCOUNT,
                            'key_file': str(KEY_FILE)} | changes))


@pytest.fixture
def registry(tmp_path):
    clock = [time.time()]
    store = Store(Settings(data_dir=tmp_path / 'data', clock=lambda: clock[0])); store.initialize()
    photo = BytesIO(); Image.new('RGB', (12, 12), 'white').save(photo, 'JPEG')
    lead = Lead(sofa='Прямой', size='Два места', material='Не знаю', description='=SUM(1,2)',
                channel='email', contact='demo@example.test')
    accepted = store.save(str(uuid.uuid4()), 'synthetic-b2', lead, [photo.getvalue()])
    return store, accepted, clock


def expected_row(registry):
    store, accepted, _ = registry
    with store.connection() as c:
        return row_for_submission(store, accepted['submission_id'], c)


def job(store, channel='sheets'):
    with store.connection() as c:
        return dict(c.execute('SELECT * FROM deliveries WHERE channel=?', (channel,)).fetchone())


def preserved(store):
    with store.connection() as c:
        rows = [tuple(r) for r in c.execute('SELECT * FROM submissions ORDER BY id')]
        rows += [tuple(r) for r in c.execute('SELECT * FROM photos ORDER BY name')]
        rows += [tuple(r) for r in c.execute("SELECT * FROM deliveries WHERE channel='telegram' ORDER BY submission_id")]
    return hashlib.sha256(repr(rows).encode()).hexdigest()


class Table:
    def __init__(self, rows=None, faults=None):
        self.rows = list(rows or [])
        self.faults = faults or {}
        self.calls = []
        self.append_count = 0

    def __call__(self, request):
        assert request.url.scheme == 'https' and request.url.host == 'sheets.googleapis.com'
        assert request.headers['authorization'] == 'Bearer ' + MARKER
        assert request.extensions['timeout'] == {'connect': 3, 'write': 5, 'read': 5, 'pool': 1}
        cell_range = unquote(request.url.path.split('/values/')[1])
        phase = 'append' if request.method == 'POST' else ('header' if cell_range == 'Leads!A1:I1'
                  else ('scan' if cell_range == f'Leads!A2:I{MAX_ROW}' else 'readback'))
        self.calls.append(phase)
        logging.getLogger('httpx').warning('Synthetic secret %s %s', MARKER, base64.b64encode(MARKER.encode()).decode())
        if phase == 'append':
            self.append_count += 1
            assert cell_range == f'Leads!A1:I{MAX_ROW}:append'
            assert request.url.params['valueInputOption'] == 'RAW'
            assert request.url.params['insertDataOption'] == 'INSERT_ROWS'
            self.rows.append(json.loads(request.content)['values'][0])
        else:
            assert request.url.params['valueRenderOption'] == 'UNFORMATTED_VALUE'
        fault = self.faults.get(phase)
        if fault:
            return fault(request, self) if callable(fault) else fault
        if phase == 'append':
            return httpx.Response(200, json={'spreadsheetId': SHEET_ID, 'updates': {
                'spreadsheetId': SHEET_ID, 'updatedRange': f'Leads!A{len(self.rows)+1}:I{len(self.rows)+1}',
                'updatedRows': 1, 'updatedColumns': 9, 'updatedCells': 9}})
        values = [HEADERS] if phase == 'header' else (self.rows if phase == 'scan' else [self.rows[-1]])
        return httpx.Response(200, json={'range': cell_range, 'majorDimension': 'ROWS', 'values': values})


def adapter(registry, table, *, live=False, config=None, auth=None):
    _, _, clock = registry
    result = SheetsAdapter(config or cfg(), auth or OfflineAuthorization(ACCOUNT, MARKER, clock[0] + 3600,
                                                                        clock=lambda: clock[0]),
                           transport=httpx.MockTransport(table))
    # Только тестовая модель live-данных; сетевой транспорт всегда MockTransport.
    result.offline = not live
    return result


def call(registry, table, *, live=False, reconcile=False, config=None, auth=None):
    store, accepted, _ = registry
    return send_one(store, accepted['submission_id'], adapter(registry, table, live=live, config=config, auth=auth),
                    reconcile=reconcile)


def error(status, retry=None):
    return httpx.Response(status, json={'error': {'code': status, 'message': MARKER}},
                          headers={'Retry-After': str(retry)} if retry is not None else {})


def raises(error_type):
    def fail(request, table):
        raise error_type(MARKER + ' ' + base64.b64encode(MARKER.encode()).decode())
    return fail


def test_raw_contract_and_secret_exclusion(registry, caplog, capsys):
    store, accepted, _ = registry; before = preserved(store); table = Table()
    row = expected_row(registry)
    with caplog.at_level(logging.DEBUG):
        result = call(registry, table)
    assert result.kind == 'delivered' and table.append_count == 1
    assert table.calls == ['header', 'scan', 'append', 'readback']
    assert table.rows == [row] and len(row) == 9
    assert row[1].endswith('Z') and row[5] == '=SUM(1,2)' and row[-1] == '1'
    assert accepted['receipt_key'] not in json.dumps(table.rows)
    receipt = store.receipt(accepted['submission_id'], accepted['receipt_key'])
    assert next(j for j in receipt['channels'] if j['channel'] == 'sheets')['mode'] == 'test'
    assert preserved(store) == before and job(store)['status'] == 'mock_delivered'
    captured = capsys.readouterr()
    for secret in (MARKER, base64.b64encode(MARKER.encode()).decode()):
        assert secret not in caplog.text + captured.out + captured.err + repr(result) + json.dumps(receipt) + job(store)['error']


@pytest.mark.parametrize('description', ['=1+2', '+SUM(1,2)', '-1+2', '@SUM(A1)', "'literal"])
def test_formula_text_and_original_utc_survive_retry(registry, description):
    store, accepted, clock = registry
    with store.connection() as c:
        data = json.loads(c.execute('SELECT payload FROM submissions').fetchone()[0]); data['description'] = description
        c.execute('UPDATE submissions SET payload=?', (json.dumps(data),))
    row = expected_row(registry)
    clock[0] += 100
    assert expected_row(registry) == row and row[5] == description
    table = Table(); assert call(registry, table).kind == 'delivered' and table.rows[0] == row


def test_found_exact_row_and_repeated_command_never_append(registry):
    table = Table([expected_row(registry)])
    assert call(registry, table).kind == 'delivered'
    assert call(registry, table).reason == 'already_confirmed'
    assert table.append_count == 0 and table.calls == ['header', 'scan']


@pytest.mark.parametrize('case', ['duplicate', 'conflict', 'headers', 'hole', 'full'])
def test_preflight_stops_without_append(registry, case):
    row = expected_row(registry); other = ['other'] + row[1:]
    rows = {'duplicate': [row, row], 'conflict': [row[:5] + ['other'] + row[6:]],
            'hole': [[''] * 9, other], 'full': [other] * 200}.get(case, [])
    table = Table(rows)
    if case == 'headers':
        table.faults['header'] = httpx.Response(200, json={'range':'Leads!A1:I1','majorDimension':'ROWS','values':[['wrong']]})
    assert call(registry, table).kind == 'failed' and table.append_count == 0


@pytest.mark.parametrize('phase', ['header', 'append', 'readback'])
@pytest.mark.parametrize('status', [401, 403, 404, 429, 500])
def test_http_policy_by_phase_and_no_secret_output(registry, phase, status, caplog):
    table = Table(faults={phase: error(status, 40 if status == 429 else None)})
    with caplog.at_level(logging.DEBUG):
        result = call(registry, table)
    expected = 'unknown' if phase == 'readback' or (phase == 'append' and status == 500) else (
        'pending' if status in {429, 500} else 'failed')
    assert result.kind == expected
    assert MARKER not in caplog.text + repr(result) + job(registry[0])['error']
    if expected == 'unknown':
        with pytest.raises(Failure):call(registry, table)
        assert table.append_count == 1


@pytest.mark.parametrize('phase', ['scan', 'append', 'readback'])
@pytest.mark.parametrize('error_type', [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteError])
def test_timeout_before_after_append_and_readback(registry, phase, error_type):
    table = Table(faults={phase: raises(error_type)})
    result = call(registry, table)
    expected = 'unknown' if phase == 'readback' or (phase == 'append' and error_type != httpx.ConnectTimeout) else 'pending'
    assert result.kind == expected


@pytest.mark.parametrize('case', ['json', 'redirect', 'encoding', 'large', 'wrong_sheet', 'wrong_id', 'wide', 'outside', 'wrong_count'])
def test_untrusted_append_ack_is_unknown(registry, case):
    if case == 'json':response = httpx.Response(200, content=b'{')
    elif case == 'redirect':response = httpx.Response(307, headers={'Location':'https://evil.invalid/'})
    elif case == 'encoding':response = httpx.Response(200, content=b'{}', headers={'Content-Encoding':'other'})
    elif case == 'large':response = httpx.Response(200, content=b'x' * (2 * 1024 * 1024 + 1))
    else:
        data = {'spreadsheetId':SHEET_ID,'updates':{'spreadsheetId':SHEET_ID,'updatedRange':'Leads!A2:I2',
                                                  'updatedRows':1,'updatedColumns':9,'updatedCells':9}}
        if case == 'wrong_sheet':data['updates']['updatedRange']='Other!A2:I2'
        if case == 'wrong_id':data['spreadsheetId']='other'
        if case == 'wide':data['updates']['updatedRange']='Leads!A2:J2'
        if case == 'outside':data['updates']['updatedRange']='Leads!A202:I202'
        if case == 'wrong_count':data['updates']['updatedCells']=True
        response=httpx.Response(200,json=data)
    table = Table(faults={'append':response})
    assert call(registry, table).kind == 'unknown'
    with pytest.raises(Failure):call(registry, table)
    assert table.append_count == 1


@pytest.mark.parametrize('retry', [None, '0', '-1', '3601', '1.5', 'date'])
def test_bad_retry_after_after_append_is_unknown(registry, retry):
    table = Table(faults={'append':error(429,retry)})
    assert call(registry, table).kind == 'unknown'


def test_retry_is_manual_delayed_and_limited_to_three(registry):
    store, _, clock = registry; table = Table(faults={'header':error(429,30)})
    for attempt in range(3):
        assert call(registry, table).kind == ('failed' if attempt == 2 else 'pending')
        with pytest.raises(Failure):call(registry, table)
        clock[0] += 31
    assert job(store)['attempts'] == 3 and table.append_count == 0


@pytest.mark.parametrize('rows_kind', ['exact', 'absent', 'conflict', 'duplicate'])
def test_unknown_reconciliation_is_read_only_and_never_grants_append(registry, rows_kind):
    store, accepted, _ = registry; table = Table(faults={'append':raises(httpx.ReadTimeout)})
    assert call(registry, table, live=True).kind == 'unknown'
    store.initialize()
    row = expected_row(registry)
    table.rows = {'exact':[row], 'absent':[], 'conflict':[row[:5]+['changed']+row[6:]], 'duplicate':[row,row]}[rows_kind]
    table.faults = {}
    result = call(registry, table, live=True, reconcile=True)
    assert result.kind == ('delivered' if rows_kind == 'exact' else 'unknown')
    assert table.calls[-2:] == ['header','scan'] and table.append_count == 1
    if rows_kind == 'exact':assert call(registry, table, live=True).reason == 'already_confirmed'
    else:
        with pytest.raises(Failure):call(registry, table, live=True)


def test_reconcile_limit_and_recipient_binding(registry):
    table = Table(faults={'append':raises(httpx.ReadTimeout)})
    assert call(registry, table).kind == 'unknown';table.rows=[];table.faults={}
    with pytest.raises(Failure):call(registry,table,reconcile=True,config=cfg(spreadsheet_id='other_spreadsheet_0123456789'))
    with pytest.raises(Failure):call(registry,table,reconcile=True,config=cfg(service_account='other-test@synthetic-project.iam.gserviceaccount.com'),
                                    auth=OfflineAuthorization('other-test@synthetic-project.iam.gserviceaccount.com',MARKER,time.time()+3600))
    for _ in range(3):assert call(registry,table,reconcile=True).kind=='unknown'
    with pytest.raises(Failure):call(registry,table,reconcile=True)
    assert table.append_count==1


def test_key_rotation_same_account_does_not_duplicate(registry):
    table=Table();assert call(registry,table).kind=='delivered'
    # Ключ не входит в binding: тот же service account с новым токеном не вызывает новый HTTP.
    auth=OfflineAuthorization(ACCOUNT,'ROTATED_SYNTHETIC_TOKEN',time.time()+3600)
    assert call(registry,table,auth=auth).reason=='already_confirmed' and table.append_count==1


def test_processing_and_post_operation_storage_failure_are_unknown(registry, monkeypatch):
    store, _, _=registry;table=Table();original=store.connection;calls=[0]
    @contextmanager
    def connection():
        calls[0]+=1
        if calls[0]==2:raise sqlite3.OperationalError(MARKER)
        with original() as c:yield c
    monkeypatch.setattr(store,'connection',connection)
    assert call(registry,table).reason=='storage_after_operation'
    monkeypatch.setattr(store,'connection',original);store.initialize()
    assert call(registry,table).kind=='unknown'
    with pytest.raises(Failure):call(registry,table)
    assert table.append_count==1


def test_expired_invalid_and_legacy_simulated_never_send(registry):
    store,accepted,clock=registry;table=Table();before=job(store)
    with pytest.raises(Failure):send_one(store,'bad',adapter(registry,table))
    clock[0]+=store.settings.retention
    with pytest.raises(Failure):call(registry,table)
    assert job(store)==before and not table.calls
    clock[0]-=store.settings.retention
    arm_test_jobs(store)
    with pytest.raises(Failure):call(registry,table,live=True)
    assert not table.calls


def test_expired_authorization_and_encoded_sdk_error_are_sanitized(registry, monkeypatch, caplog, capsys):
    _,_,clock=registry;table=Table()
    auth=OfflineAuthorization(ACCOUNT,MARKER,clock[0],clock=lambda:clock[0])
    assert call(registry,table,auth=auth).kind=='failed' and not table.calls
    # SDK отсутствует: проверяется контракт исключения искусственного provider, не настоящий SDK.
    def fail(self):raise RuntimeError(json.dumps({'private_key':MARKER,'assertion':base64.b64encode(MARKER.encode()).decode()}))
    monkeypatch.setattr(OfflineAuthorization,'token',fail)
    with caplog.at_level(logging.DEBUG):
        result=adapter(registry,table).execute(expected_row(registry))
    captured=capsys.readouterr()
    assert result.kind=='failed'
    for secret in (MARKER,base64.b64encode(MARKER.encode()).decode()):
        assert secret not in caplog.text+captured.out+captured.err+repr(result)


@pytest.mark.parametrize('telegram_state', ['not_connected','live_delivered','live_unknown','test'])
@pytest.mark.parametrize('sheets_state', ['live_delivered','live_unknown','test'])
def test_independent_channel_receipts(registry, telegram_state, sheets_state):
    store,accepted,_=registry
    with store.connection() as c:
        c.execute("UPDATE deliveries SET status=?,simulated=? WHERE channel='telegram'",
                  ('delivered' if telegram_state=='test' else telegram_state,int(telegram_state=='test')))
    before=preserved(store)
    table=Table(faults={'append':raises(httpx.ReadTimeout)} if sheets_state=='live_unknown' else {})
    call(registry,table,live=sheets_state!='test')
    receipt=store.receipt(accepted['submission_id'],accepted['receipt_key']);message=receipt['message']
    assert preserved(store)==before
    if sheets_state=='live_delivered':assert 'подтверждена чтением' in message and 'Google Sheets не подключены' not in message
    if sheets_state=='live_unknown':assert 'повтор append запрещён' in message and 'подтверждена чтением' not in message
    if telegram_state=='live_delivered':assert 'создание сообщения' in message and 'прочтение человеком не подтверждено' in message
    if telegram_state=='live_unknown':assert 'Telegram неизвестен' in message
    assert 'Реальных отправок нет' not in message


@pytest.mark.parametrize('text', ['export D4_SHEETS_LIVE=1','D4_SHEETS_LIVE=$(x)', 'UNKNOWN=x',
                                 'D4_SHEETS_LIVE=0\nD4_SHEETS_LIVE=0','D4_SHEETS_SHEET=Other',
                                 'D4_SHEETS_KEY_FILE=/tmp/other.json'])
def test_strict_configuration(text):
    with pytest.raises(ConfigurationError):parse_config(text)


def test_template_private_loader_and_missing_sdk(tmp_path, monkeypatch):
    assert not parse_config((ROOT/'.env.sheets.example').read_text()).enabled
    assert MARKER not in repr(cfg())+repr(OfflineAuthorization(ACCOUNT,MARKER,time.time()+3600))
    file=tmp_path/'synthetic.env';file.write_text('D4_SHEETS_LIVE=0');file.chmod(0o600)
    assert not load_config(file).enabled
    file.chmod(0o644)
    with pytest.raises(ConfigurationError):load_config(file)
    link=tmp_path/'link';link.symlink_to(file)
    with pytest.raises(ConfigurationError):load_config(link)
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS',str(file))
    with pytest.raises(ConfigurationError,match='SDK'):require_live_sdk()
    patterns=(ROOT/'.gitignore').read_text().splitlines()
    def ignored(name):
        value=False
        for pattern in patterns:
            if pattern and not pattern.startswith('#') and fnmatch.fnmatchcase(name,pattern.lstrip('!')):value=not pattern.startswith('!')
        return value
    assert not ignored('.env.sheets.example') and ignored('.env.sheets') and ignored('.env.sheets.backup')


@pytest.mark.parametrize('field,value', [('type','authorized_user'),('client_email','other'),('token_uri','https://evil.invalid'),
                                        ('universe_domain','evil.invalid'),('subject','user'),('auth_uri','https://evil.invalid')])
def test_service_account_validation_before_sdk(field,value):
    info={'type':'service_account','client_email':ACCOUNT,'private_key':MARKER,'token_uri':'https://oauth2.googleapis.com/token'}
    validate_service_account(info,cfg())
    with pytest.raises(ConfigurationError):validate_service_account(info|{field:value},cfg())


@pytest.mark.parametrize('command', ['sheets-send','sheets-reconcile'])
@pytest.mark.parametrize('execute', [False,True])
def test_cli_without_sdk_never_reads_config_or_modifies_registry(registry,monkeypatch,command,execute,capsys):
    import server.manage as manage
    import server.sheets_config as config_module
    def forbidden(*args,**kwargs):pytest.fail('Configuration/real registry must not be opened')
    monkeypatch.setattr(manage,'Store',forbidden);monkeypatch.setattr(config_module,'load_config',forbidden)
    monkeypatch.setattr(sys,'argv',['manage',command,'--submission-id',registry[1]['submission_id']]+(['--execute-live'] if execute else []))
    with pytest.raises(SystemExit) as result:manage.main()
    assert result.value.code==2 and 'секреты не читались' in capsys.readouterr().err.lower()


CLI_HARNESS = r'''
import json,runpy,socket,sys,time
from pathlib import Path
import httpx
from server import sheets,sheets_config
def deny(*a,**kw):raise AssertionError('External sockets forbidden')
socket.getaddrinfo=socket.create_connection=deny
socket.socket.connect=deny
root=Path.cwd();mode=sys.argv[1];sid=sys.argv[2]
sheets_config.require_live_sdk=lambda:None
def handler(request):
    cell=request.url.path.split('/values/')[1]
    if mode=='hold' and cell=='Leads!A1:I1':
        (root/'entered').touch();deadline=time.monotonic()+8
        while not (root/'release').exists():
            if time.monotonic()>deadline:raise RuntimeError('synthetic timeout')
            time.sleep(.02)
    rows_file=root/'rows.json'
    rows=json.loads(rows_file.read_text()) if rows_file.exists() else []
    if request.method=='POST':
        calls_file=root/'append-count'
        count=int(calls_file.read_text()) if calls_file.exists() else 0
        calls_file.write_text(str(count+1))
        rows.append(json.loads(request.content)['values'][0]);rows_file.write_text(json.dumps(rows))
        return httpx.Response(200,json={'spreadsheetId':'synthetic_spreadsheet_0123456789','updates':{
            'spreadsheetId':'synthetic_spreadsheet_0123456789','updatedRange':'Leads!A2:I2',
            'updatedRows':1,'updatedColumns':9,'updatedCells':9}})
    values=[sheets.HEADERS] if cell=='Leads!A1:I1' else rows
    return httpx.Response(200,json={'range':cell,'majorDimension':'ROWS','values':values})
def build(config):
    result=sheets.SheetsAdapter(config,sheets.OfflineAuthorization(config.service_account,'SYNTHETIC_CLI_BEARER',time.time()+3600),transport=httpx.MockTransport(handler))
    result.offline=False
    return result
sheets.build_live_adapter=build
sys.argv=['server.manage','sheets-send','--submission-id',sid,'--execute-live']
runpy.run_module('server.manage',run_name='__main__')
'''


def cli_root(tmp_path, *, configured=True):
    shutil.copytree(ROOT/'server',tmp_path/'server',ignore=shutil.ignore_patterns('__pycache__'))
    store=Store(Settings(data_dir=tmp_path/'data'));store.initialize()
    image=BytesIO();Image.new('RGB',(12,12),'white').save(image,'JPEG')
    lead=Lead(sofa='Прямой',size='Два места',material='Не знаю',description='=1+2',channel='email',contact='demo@example.test')
    accepted=store.save(str(uuid.uuid4()),'cli-b2',lead,[image.getvalue()])
    if configured:
        text=(f'D4_SHEETS_SPREADSHEET_ID={SHEET_ID}\nD4_SHEETS_SERVICE_ACCOUNT={ACCOUNT}\n'
              f'D4_SHEETS_KEY_FILE={tmp_path}/.secrets/sheets-service-account.json\nD4_SHEETS_LIVE=1\n')
        path=tmp_path/'.env.sheets';path.write_text(text);path.chmod(0o600)
    return store,accepted


def test_cli_no_config_after_mock_sdk_still_stops_without_http(tmp_path):
    store,accepted=cli_root(tmp_path,configured=False);before=job(store)
    result=subprocess.run([sys.executable,'-c',CLI_HARNESS,'success',accepted['submission_id']],cwd=tmp_path,
                          env={'PATH':os.defpath},capture_output=True,text=True,timeout=10)
    assert result.returncode==2 and 'Приватный файл Sheets недоступен' in result.stderr
    assert job(store)==before and not (tmp_path/'append-count').exists()


def test_parallel_cli_and_backup_share_lock_and_repeat_does_not_append(tmp_path):
    store,accepted=cli_root(tmp_path);before=preserved(store)
    command=lambda mode:[sys.executable,'-c',CLI_HARNESS,mode,accepted['submission_id']]
    env={'PATH':os.defpath}
    first=subprocess.Popen(command('hold'),cwd=tmp_path,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+8
        while not (tmp_path/'entered').exists():
            if first.poll() is not None or time.monotonic()>deadline:pytest.fail('Mock Sheets did not reach protected read')
            time.sleep(.02)
        second=subprocess.run(command('success'),cwd=tmp_path,env=env,capture_output=True,text=True,timeout=5)
        backup=subprocess.run([sys.executable,'-m','server.manage','backup'],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=5)
        assert second.returncode==backup.returncode==2 and not (tmp_path/'backups').exists()
        (tmp_path/'release').touch();out,err=first.communicate(timeout=8)
        assert first.returncode==0 and 'подтверждена чтением' in out
        repeat=subprocess.run(command('success'),cwd=tmp_path,env=env,capture_output=True,text=True,timeout=5)
        assert repeat.returncode==0 and (tmp_path/'append-count').read_text()=='1'
        assert preserved(store)==before and job(store)['status']=='live_delivered'
        assert MARKER not in out+err+repeat.stdout+repeat.stderr
    finally:
        (tmp_path/'release').touch()
        if first.poll() is None:first.kill();first.wait(timeout=3)


@pytest.mark.parametrize('telegram_state,sheets_state', [('live_delivered','live_delivered'),('live_unknown','live_delivered'),
                                                       ('live_delivered','live_unknown'),('test','live_delivered')])
def test_actual_render_receipt_keeps_channels_independent(registry,telegram_state,sheets_state):
    store,accepted,_=registry
    with store.connection() as c:
        c.execute("UPDATE deliveries SET status=?,simulated=? WHERE channel='telegram'",
                  ('delivered' if telegram_state=='test' else telegram_state,int(telegram_state=='test')))
        c.execute("UPDATE deliveries SET status=?,simulated=0 WHERE channel='sheets'",(sheets_state,))
    receipt=store.receipt(accepted['submission_id'],accepted['receipt_key'])
    script=r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const fn=source.slice(source.indexOf('function renderReceipt(data)'),source.indexOf("document.querySelector('#refresh-receipt')"));
const make=()=>({textContent:'',hidden:false,children:[],append(...v){this.children.push(...v)},replaceChildren(){this.children=[]},focus(){}});
const nodes=Object.fromEntries(['#result-title','#receipt-details'].map(id=>[id,make()]));
const data=JSON.parse(fs.readFileSync(0,'utf8'));
const context={document:{querySelector:id=>nodes[id],createElement:make},summary:make(),result:make(),data};
vm.createContext(context);vm.runInContext(fn+';renderReceipt(data);',context);
process.stdout.write(JSON.stringify({title:nodes['#result-title'].textContent,notes:nodes['#receipt-details'].children.map(n=>n.textContent)}));
'''
    result=subprocess.run(['node','-e',script,str(ROOT/'public/app.js')],input=json.dumps(receipt),text=True,capture_output=True,check=True,timeout=10)
    ui=json.loads(result.stdout);assert ui['title']==receipt['message']
    assert 'Google Sheets не подключены' not in ui['title'] and 'Реальных отправок нет' not in json.dumps(ui,ensure_ascii=False)
    notes=[n for n in ui['notes'] if 'тестовый режим' in n]
    assert len(notes)==(1 if telegram_state=='test' else 0)
    if notes:assert notes[0].startswith('Telegram:')


def test_sdk_presence_alone_does_not_enable_live(monkeypatch):
    import server.sheets_config as module
    from server.sheets import build_live_adapter
    monkeypatch.setattr(module.importlib.metadata,'version',lambda name:'synthetic-installed')
    with pytest.raises(ConfigurationError,match='ещё не реализован'):require_live_sdk()
    with pytest.raises(ConfigurationError,match='закрыт'):build_live_adapter(cfg(enabled=True))


def test_http_startup_and_post_do_not_create_sheets_auth_or_transport(registry,monkeypatch):
    from fastapi.testclient import TestClient
    from server.app import create_app
    import server.sheets as sheets_module
    import server.sheets_config as config_module
    def forbidden(*args,**kwargs):pytest.fail('HTTP must only save, not invoke Sheets')
    monkeypatch.setattr(sheets_module,'build_live_adapter',forbidden)
    monkeypatch.setattr(sheets_module.SheetsAdapter,'__init__',forbidden)
    monkeypatch.setattr(config_module,'require_live_sdk',forbidden)
    monkeypatch.setattr(config_module,'load_config',forbidden)
    store,_,_=registry
    with store.connection() as c:
        fields=json.loads(c.execute('SELECT payload FROM submissions').fetchone()[0])
        photo=c.execute('SELECT content FROM photos').fetchone()[0]
    with TestClient(create_app(store.settings),base_url='http://127.0.0.1') as client:
        assert client.get('/').status_code==200
        result=client.post('/api/submissions',data=fields,files={'photos':('synthetic.jpg',photo,'image/jpeg')},
                           headers={'Idempotency-Key':str(uuid.uuid4())})
        assert result.status_code==201
