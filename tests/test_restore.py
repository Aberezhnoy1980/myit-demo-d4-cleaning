"""Отрицательные restore-опыты только в tmp_path, без настоящих data/backups."""
from io import BytesIO
from contextlib import closing
from pathlib import Path
import hashlib
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from server.app import create_app
from server.model import Lead, ROOT, Settings
from server.store import SCHEMA, Store


def jpeg(large=False):
    out = BytesIO()
    image = (Image.frombytes('RGB', (512, 512), random.Random(14).randbytes(512 * 512 * 3))
             if large else Image.new('RGB', (12, 12), 'green'))
    image.save(out, format='JPEG', quality=90)
    return out.getvalue()


def seed(root, large=False, processing=False):
    now = time.time()
    store = Store(Settings(data_dir=root / 'data', clock=lambda: now))
    store.initialize()
    lead = Lead(sofa='Прямой', size='Два места', material='Ткань / рогожка',
                description='Синтетическая проверка сохранности после отказа restore.',
                channel='email', contact='demo@example.test')
    accepted = store.save(str(uuid.uuid4()), 'synthetic-hash', lead, [jpeg(large)])
    with store.connection() as c:
        c.execute("UPDATE deliveries SET simulated=1,status=?,attempts=1 WHERE channel='telegram'",
                  ('processing' if processing else 'unknown',))
        c.execute('INSERT INTO rate_events VALUES(?)', (now,))
    return store, accepted


def database_state(path):
    """Полные строки/байты проверяются хэшами; ключи квитанций не попадают в отчёт."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as c:
        result = {}
        for table in ('submissions', 'photos', 'deliveries', 'rate_events'):
            rows = c.execute('SELECT * FROM ' + table + ' ORDER BY 1,2' if table != 'rate_events'
                             else 'SELECT * FROM rate_events ORDER BY stamp').fetchall()
            result[table] = {'count': len(rows), 'sha256': hashlib.sha256(repr(rows).encode()).hexdigest()}
        result['integrity'] = c.execute('PRAGMA integrity_check').fetchall()
    return result


def assert_preserved(store, accepted, before, receipt):
    assert database_state(store.path) == before
    assert store.receipt(accepted['submission_id'], accepted['receipt_key']) == receipt
    assert not list(store.settings.data_dir.glob('.restore-*'))


INVALID_CASES = ['columns', 'unique', 'not_null', 'foreign_key', 'default', 'trigger', 'corrupt', 'orphan', 'version']


def invalid_backup(path, case):
    path.parent.mkdir(parents=True, exist_ok=True)
    if case == 'corrupt':
        path.write_bytes(b'not a SQLite database\0synthetic corrupted backup')
        return
    with sqlite3.connect(path) as c:
        if case == 'columns':
            for table in ('submissions', 'photos', 'deliveries', 'rate_events'):
                c.execute('CREATE TABLE ' + table + '(unrelated TEXT)')
        else:
            schema = SCHEMA
            if case == 'unique': schema = schema.replace('request_key TEXT NOT NULL UNIQUE', 'request_key TEXT NOT NULL')
            if case == 'not_null': schema = schema.replace('body_hash TEXT NOT NULL', 'body_hash TEXT')
            if case == 'foreign_key': schema = schema.replace(' ON DELETE CASCADE', '')
            if case == 'default': schema = schema.replace("DEFAULT 'not_connected'", "DEFAULT 'pending'")
            c.executescript(schema)
            if case == 'trigger':
                c.execute("CREATE TRIGGER extra BEFORE UPDATE ON deliveries BEGIN SELECT RAISE(ABORT,'synthetic trigger'); END")
            if case == 'orphan':
                c.execute("INSERT INTO photos VALUES('orphan.jpg','missing',?)", (jpeg(),))
            if case == 'version': c.execute('PRAGMA user_version=9')
    os.chmod(path, 0o600)


def candidate_backup(root):
    candidate, accepted = seed(root / 'candidate', large=True, processing=True)
    source = root / 'backups' / 'candidate.sqlite3'
    candidate.backup(source)
    return source, accepted


def fault_connection(original, working, observed):
    """Исключение после реального переноса первых 16 страниц, до SQLITE_DONE."""
    class Interrupted(sqlite3.Connection):
        def backup(self, target, *, progress=None, **kwargs):
            target_path = Path(target.execute('PRAGMA database_list').fetchone()[2])
            def interrupted(status, remaining, total):
                if target_path == working and status == sqlite3.SQLITE_OK and 0 < remaining < total:
                    observed.append({'remaining': remaining, 'total': total})
                    raise sqlite3.OperationalError('synthetic partial restore failure')
                if progress:
                    progress(status, remaining, total)
            return super().backup(target, progress=interrupted, **kwargs)
    return lambda *args, **kwargs: original(*args, factory=Interrupted, **kwargs)


@pytest.mark.parametrize('case', INVALID_CASES)
def test_store_rejects_invalid_backup_preserves_everything(tmp_path, case):
    store, accepted = seed(tmp_path)
    before = database_state(store.path)
    receipt = store.receipt(accepted['submission_id'], accepted['receipt_key'])
    source = tmp_path / 'backups' / (case + '.sqlite3')
    invalid_backup(source, case)
    with pytest.raises((ValueError, sqlite3.Error)):
        store.restore(source)
    assert_preserved(store, accepted, before, receipt)
    # Квитанция доступна также через настоящий обработчик HTTP приложения.
    with TestClient(create_app(store.settings), base_url='http://127.0.0.1') as client:
        response = client.get('/api/receipts/' + accepted['submission_id'],
                              headers={'X-Receipt-Key': accepted['receipt_key']})
        assert response.status_code == 200 and response.json() == receipt


@pytest.mark.parametrize('phase', ['initialize', 'cleanup'])
def test_store_preparation_failure_does_not_change_working_registry(tmp_path, monkeypatch, phase):
    store, accepted = seed(tmp_path)
    before, receipt = database_state(store.path), store.receipt(accepted['submission_id'], accepted['receipt_key'])
    source, _ = candidate_backup(tmp_path)
    original = getattr(Store, phase)
    observed = []
    def failing(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.path != store.path and '.restore-' in str(self.path):
            with self.connection() as c:
                c.execute('DELETE FROM submissions')  # подготовленная копия уже изменилась
            observed.append(True)
            raise sqlite3.OperationalError('synthetic preparation failure')
        return result
    monkeypatch.setattr(Store, phase, failing)
    with pytest.raises(sqlite3.Error):
        store.restore(source)
    assert observed
    assert_preserved(store, accepted, before, receipt)


def test_store_partial_copy_failure_rolls_back_all_pages(tmp_path, monkeypatch):
    store, accepted = seed(tmp_path)
    source, _ = candidate_backup(tmp_path)
    before, receipt = database_state(store.path), store.receipt(accepted['submission_id'], accepted['receipt_key'])
    observed = []
    monkeypatch.setattr(sqlite3, 'connect', fault_connection(sqlite3.connect, store.path, observed))
    with pytest.raises(sqlite3.Error):
        store.restore(source)
    assert observed and observed[0]['remaining'] < observed[0]['total']
    assert_preserved(store, accepted, before, receipt)
    # После отката реестр не только читается, но и принимает новую заявку.
    with store.connection() as c:
        payload = c.execute('SELECT payload FROM submissions LIMIT 1').fetchone()[0]
    store.save(str(uuid.uuid4()), 'another-synthetic-hash', Lead.model_validate_json(payload), [jpeg()])
    assert database_state(store.path)['submissions']['count'] == 2


def test_successful_restore_prepares_snapshot_before_publish(tmp_path):
    store, old = seed(tmp_path)
    source, accepted = candidate_backup(tmp_path)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store.restore(source)
    receipt = store.receipt(accepted['submission_id'], accepted['receipt_key'])
    assert receipt['photos'] == 1
    assert next(j for j in receipt['channels'] if j['channel'] == 'telegram')['status'] == 'unknown'
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert database_state(store.path)['submissions']['count'] == 1
    assert not list(store.settings.data_dir.glob('.restore-*'))


@pytest.mark.parametrize('case', ['columns', 'unique', 'foreign_key', 'corrupt', 'initialize', 'cleanup', 'partial_copy'])
def test_cli_failure_in_separate_process_preserves_registry_and_safety_backup(tmp_path, case):
    # Копия только исходного server/, ROOT/Settings у CLI указывают в tmp_path.
    shutil.copytree(ROOT / 'server', tmp_path / 'server', ignore=shutil.ignore_patterns('__pycache__'))
    store, accepted = seed(tmp_path, processing=True)
    before, receipt = database_state(store.path), store.receipt(accepted['submission_id'], accepted['receipt_key'])
    source = tmp_path / 'backups' / 'candidate.sqlite3'
    if case in {'initialize', 'cleanup', 'partial_copy'}:
        source, _ = candidate_backup(tmp_path)
    else:
        invalid_backup(source, case)
    # Fault injection только в одноразовом процессе теста, без production flags/endpoint.
    harness = '''
from pathlib import Path
import runpy,sqlite3,sys,json
from server.store import Store
from server.model import ROOT
assert ROOT == Path.cwd()
case=sys.argv[1]
observed=[]
if case in {'initialize','cleanup'}:
    original=getattr(Store,case)
    def failing(self,*args,**kwargs):
        result=original(self,*args,**kwargs)
        if '.restore-' in str(self.path):
            observed.append(True)
            raise sqlite3.OperationalError('synthetic preparation failure')
        return result
    setattr(Store,case,failing)
if case=='partial_copy':
    original_connect=sqlite3.connect
    class Interrupted(sqlite3.Connection):
        def backup(self,target,*,progress=None,**kwargs):
            destination=Path(target.execute('PRAGMA database_list').fetchone()[2])
            def interrupted(status,remaining,total):
                if destination==ROOT/'data/registry.sqlite3' and status==sqlite3.SQLITE_OK and 0<remaining<total:
                    observed.append({'remaining':remaining,'total':total})
                    raise sqlite3.OperationalError('synthetic partial restore failure')
                if progress: progress(status,remaining,total)
            return super().backup(target,progress=interrupted,**kwargs)
    sqlite3.connect=lambda *args,**kwargs:original_connect(*args,factory=Interrupted,**kwargs)
sys.argv=['server.manage','restore','candidate.sqlite3']
try:
    runpy.run_module('server.manage',run_name='__main__')
finally:
    (ROOT/'fault-observed.json').write_text(json.dumps(observed))
'''
    command = ([sys.executable, '-c', harness, case] if case in {'initialize', 'cleanup', 'partial_copy'}
               else [sys.executable, '-m', 'server.manage', 'restore', 'candidate.sqlite3'])
    result = subprocess.run(command, cwd=tmp_path,
                            env=os.environ | {'PYTHONPATH': '', 'PYTHONNOUSERSITE': '1'},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert 'Восстановление не выполнено' in result.stderr and 'Traceback' not in result.stderr
    assert 'Восстановлено.' not in result.stdout
    if case in {'initialize', 'cleanup', 'partial_copy'}:
        assert json.loads((tmp_path / 'fault-observed.json').read_text())
    assert_preserved(store, accepted, before, receipt)
    safety = list((tmp_path / 'backups').glob('before-restore-*.sqlite3'))
    assert len(safety) == 1 and database_state(safety[0]) == before
    assert safety[0].stat().st_mode & 0o777 == 0o600


def test_real_cli_success_prepares_restore_and_keeps_safety_backup(tmp_path):
    shutil.copytree(ROOT / 'server', tmp_path / 'server', ignore=shutil.ignore_patterns('__pycache__'))
    store, old = seed(tmp_path)
    before = database_state(store.path)
    source, accepted = candidate_backup(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'server.manage', 'restore', 'candidate.sqlite3'],
                            cwd=tmp_path, env=os.environ | {'PYTHONPATH': '', 'PYTHONNOUSERSITE': '1'},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and 'Восстановлено.' in result.stdout and not result.stderr
    receipt = store.receipt(accepted['submission_id'], accepted['receipt_key'])
    assert receipt['photos'] == 1
    assert next(j for j in receipt['channels'] if j['channel'] == 'telegram')['status'] == 'unknown'
    safety = list((tmp_path / 'backups').glob('before-restore-*.sqlite3'))
    assert len(safety) == 1 and database_state(safety[0]) == before
    assert not list(store.settings.data_dir.glob('.restore-*'))
