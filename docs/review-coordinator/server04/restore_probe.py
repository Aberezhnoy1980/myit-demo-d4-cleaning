"""Independent failure probe; operates only in its own temporary directory."""
from pathlib import Path
import sys
import json
import sqlite3
import tempfile
import uuid
import hashlib
from io import BytesIO

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from PIL import Image
from server.model import Settings, Lead
from server.store import Store

results = []
def check(name, passed, detail=None):
    results.append(dict(name=name, passed=bool(passed), detail=detail))

with tempfile.TemporaryDirectory(prefix='d4-restore-review-') as temporary:
    folder = Path(temporary)
    store = Store(Settings(data_dir=folder / 'data'))
    store.initialize()
    lead = Lead(sofa='Прямой', size='Два места', material='Ткань / рогожка',
                description='Синтетический тест восстановления.', channel='email', contact='demo@example.test')
    image = BytesIO()
    Image.new('RGB', (12, 12), 'white').save(image, 'JPEG')
    accepted = store.save(str(uuid.uuid4()), 'synthetic-hash', lead, [image.getvalue()])
    sid = accepted['submission_id']
    valid = folder / 'backup' / 'valid.sqlite3'
    store.backup(valid)
    store.save(str(uuid.uuid4()), 'other-synthetic-hash', lead, [image.getvalue()])
    store.restore(valid)
    with store.connection() as c:
        check('valid backup restores original record', c.execute('SELECT count(*) FROM submissions').fetchone()[0] == 1)

    def full_snapshot():
        with store.connection() as c:
            state = [c.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()
                     for table in ['submissions','photos','deliveries','rate_events']]
            return hashlib.sha256(repr([[tuple(row) for row in rows] for rows in state]).encode()).hexdigest()
    with store.connection() as c:
        c.execute("UPDATE deliveries SET status='processing' WHERE channel='telegram'")
    before = full_snapshot()
    before_receipt = store.receipt(sid, accepted['receipt_key'])

    invalid = folder / 'backup' / 'wrong-schema.sqlite3'
    with sqlite3.connect(invalid) as c:
        for table in ['submissions', 'photos', 'deliveries', 'rate_events']:
            c.execute(f'CREATE TABLE {table}(unrelated TEXT)')
        check('invalid-schema database integrity ok', c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok')
    caught = None
    try:
        store.restore(invalid)
    except Exception as error:
        caught = type(error).__name__ + ': ' + str(error)
    check('invalid schema rejected', caught is not None, caught)
    try:
        with store.connection() as c:
            original_survives = c.execute('SELECT count(*) FROM submissions WHERE id=?', (sid,)).fetchone()[0] == 1
    except sqlite3.Error:
        original_survives = False
    check('working registry preserved after rejection', original_survives)
    check('all rows and photo bytes unchanged', full_snapshot() == before)
    check('original receipt still works and unchanged', store.receipt(sid, accepted['receipt_key']) == before_receipt)

output = Path(__file__).with_name('restore-probe.json')
output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(results, ensure_ascii=False))
