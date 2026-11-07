"""Независимые сценарии B2: временная БД, искусственная авторизация, без сети."""
from io import BytesIO
from pathlib import Path
import hashlib
import json
import socket
import sys
import tempfile
import time
from urllib.parse import unquote
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import httpx
from PIL import Image
from server.model import Failure, Lead, Settings
from server.store import Store
from server.sheets import HEADERS, OfflineAuthorization, SheetsAdapter, send_one
from server.sheets_config import SheetsConfig, KEY_FILE


def deny(*args, **kwargs):
    raise AssertionError('External network forbidden')


socket.create_connection = socket.getaddrinfo = deny
socket.socket.connect = deny
config = SheetsConfig('independent_spreadsheet_012345',
                      'reviewer@synthetic-project.iam.gserviceaccount.com', str(KEY_FILE))
checks = {}


def snapshot(store):
    with store.connection() as c:
        data = [tuple(r) for r in c.execute('SELECT * FROM submissions')]
        data += [tuple(r) for r in c.execute('SELECT * FROM photos')]
        data += [tuple(r) for r in c.execute("SELECT * FROM deliveries WHERE channel='telegram'")]
    return hashlib.sha256(repr(data).encode()).hexdigest()


with tempfile.TemporaryDirectory(prefix='d4-b2-independent-') as tmp:
    for scenario in ['lost_append_response', 'lost_local_commit', 'conflicting_reconciliation']:
        store = Store(Settings(data_dir=Path(tmp) / scenario))
        store.initialize()
        photo = BytesIO()
        Image.new('RGB', (12, 12), 'white').save(photo, 'JPEG')
        lead = Lead(sofa='Прямой', size='Два места', material='Не знаю',
                    description='=1+2', channel='email', contact='demo@example.test')
        accepted = store.save(str(uuid.uuid4()), 'independent', lead, [photo.getvalue()])
        sid = accepted['submission_id']
        with store.connection() as c:
            c.execute("UPDATE deliveries SET status='live_delivered',attempts=1 WHERE channel='telegram'")
        before = snapshot(store)
        rows = []
        methods = []

        def handler(request):
            methods.append(request.method)
            cell_range = unquote(request.url.path.split('/values/')[1])
            if request.method == 'POST':
                assert request.url.params['valueInputOption'] == 'RAW'
                rows.append(json.loads(request.content)['values'][0])
                assert rows[0][5] == '=1+2'
                if scenario != 'lost_local_commit':
                    raise httpx.ReadTimeout('synthetic lost append response')
                with store.connection() as c:
                    c.execute("CREATE TRIGGER fail_result BEFORE UPDATE ON deliveries WHEN NEW.channel='sheets' AND NEW.status='mock_delivered' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
                return httpx.Response(200, json={'spreadsheetId': config.spreadsheet_id, 'updates': {
                    'spreadsheetId': config.spreadsheet_id, 'updatedRange': 'Leads!A2:I2',
                    'updatedRows': 1, 'updatedColumns': 9, 'updatedCells': 9}})
            values = [HEADERS] if cell_range == 'Leads!A1:I1' else rows
            return httpx.Response(200, json={'range': cell_range, 'majorDimension': 'ROWS', 'values': values})

        adapter = SheetsAdapter(config, OfflineAuthorization(config.service_account,
            'INDEPENDENT_SYNTHETIC_BEARER', time.time()+3600), transport=httpx.MockTransport(handler))
        assert send_one(store, sid, adapter).kind == 'unknown'
        if scenario == 'lost_local_commit':
            with store.connection() as c:
                c.execute('DROP TRIGGER fail_result')
        store.initialize()
        initial = len(methods)
        try:
            outcome = send_one(store, sid, adapter)
            assert outcome.kind == 'unknown'  # processing → unknown, без HTTP
        except Failure as error:
            assert error.status == 409
        assert len(methods) == initial
        if scenario == 'conflicting_reconciliation':
            rows[0][5] = 'different synthetic payload'
        outcome = send_one(store, sid, adapter, reconcile=True)
        assert outcome.kind == ('unknown' if scenario == 'conflicting_reconciliation' else 'delivered')
        assert methods[initial:] == ['GET', 'GET'] and methods.count('POST') == 1
        assert len(rows) == 1 and snapshot(store) == before
        checks[scenario] = {'passed': True, 'append_requests': 1, 'read_only_reconciliation': True,
                            'telegram_submission_photo_preserved': True}

(Path(__file__).parent / 'probe-results.json').write_text(json.dumps(checks, indent=2)+'\n')
print(json.dumps(checks))
