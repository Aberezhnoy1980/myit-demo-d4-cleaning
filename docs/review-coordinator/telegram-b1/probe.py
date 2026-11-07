"""Независимая проба: временная БД, искусственный бот, только MockTransport."""
from io import BytesIO
from pathlib import Path
import json
import socket
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import httpx
from PIL import Image
from server.model import Lead, Settings
from server.store import Store
from server.delivery import arm_test_jobs, step, TestAdapter
from server.telegram import TelegramAdapter, send_one
from server.telegram_config import TelegramConfig


def deny(*args, **kwargs):
    raise AssertionError('External network forbidden')


socket.create_connection = socket.getaddrinfo = deny
checks = {}
calls = []
with tempfile.TemporaryDirectory(prefix='d4-b1-review-') as directory:
    settings = Settings(data_dir=Path(directory) / 'data')
    store = Store(settings)
    store.initialize()
    photo = BytesIO()
    Image.new('RGB', (16, 16), 'white').save(photo, 'JPEG')
    lead = Lead(sofa='Прямой', size='Два места', material='Не знаю',
                description='Независимая синтетическая проба.', channel='email', contact='demo@example.test')
    accepted = store.save(str(uuid.uuid4()), 'independent', lead, [photo.getvalue()])
    sid = accepted['submission_id']

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={'ok': True, 'result': {
            'message_id': 29, 'chat': {'id': -1009876543210},
            'photo': [{'file_id': 'synthetic', 'width': 16, 'height': 16}]}})

    cfg = TelegramConfig('987654321:INDEPENDENT_SYNTHETIC_TOKEN_XXXXXXXXXX', '-1009876543210', True)
    with patch.object(httpx, 'HTTPTransport', lambda **kwargs: httpx.MockTransport(handler)):
        adapter = TelegramAdapter(cfg, allow_live=True)
        assert send_one(store, sid, adapter).kind == 'delivered'
        reopened = Store(settings)
        reopened.initialize()
        assert send_one(reopened, sid, adapter).reason == 'already_created' and len(calls) == 1
        checks['restart_does_not_resend'] = True
        rotated = TelegramConfig('987654321:ROTATED_SYNTHETIC_TOKEN_XXXXXXXXXXXX', cfg.chat_id, True)
        assert send_one(reopened, sid, TelegramAdapter(rotated, allow_live=True)).reason == 'already_created'
        assert len(calls) == 1
        checks['same_bot_token_rotation_does_not_resend'] = True

    # Поддержанная команда simulate для Sheets после подтверждённого Telegram.
    arm_test_jobs(reopened)
    assert step(reopened, 'sheets', TestAdapter('success')) == 'delivered'
    receipt = reopened.receipt(sid, accepted['receipt_key'])
    channels = {job['channel']: job for job in receipt['channels']}
    assert channels['telegram']['status'] == 'live_delivered' and channels['telegram']['simulated'] == 0
    assert channels['sheets']['simulated'] == 1
    checks['sheets_simulation_preserves_live_telegram'] = True

    # Исполняется фактическая функция UI; DOM заменён минимальными узлами.
    js = r'''
const fs = require('fs'); const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const fn = source.slice(source.indexOf('function renderReceipt(data)'), source.indexOf("document.querySelector('#refresh-receipt')"));
const make = () => ({textContent:'', hidden:false, children:[], append(...v){this.children.push(...v)}, replaceChildren(){this.children=[]}, focus(){}});
const nodes = Object.fromEntries(['#result-title','#receipt-details'].map(id=>[id,make()]));
const receipt = JSON.parse(fs.readFileSync(0,'utf8'));
const context = {document:{querySelector:id=>nodes[id],createElement:make},summary:make(),result:make(),receipt};
vm.createContext(context);vm.runInContext(fn+';renderReceipt(receipt);',context);
process.stdout.write(JSON.stringify({title:nodes['#result-title'].textContent, notes:nodes['#receipt-details'].children.map(n=>n.textContent)}));
'''
    rendered = subprocess.run(['fnm', 'exec', '--using=v22.23.4', '--', 'node', '-e', js,
                               str(ROOT / 'public/app.js')], input=json.dumps(receipt),
                              text=True, capture_output=True, check=True, timeout=15)
    ui = json.loads(rendered.stdout)
    checks['mixed_receipt_falsely_denies_real_sends'] = any('Реальных отправок нет' in note for note in ui['notes'])
    assert checks['mixed_receipt_falsely_denies_real_sends']
    result = {'checks': checks, 'telegram_http_calls': len(calls),
              'transport': 'MockTransport only; network forbidden',
              'channels': receipt['channels'], 'rendered': ui,
              'finding': 'P2: live Telegram + simulated Sheets противоречат общему сообщению UI об отсутствии отправок.'}
    (Path(__file__).parent / 'probe-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(checks, ensure_ascii=False))
