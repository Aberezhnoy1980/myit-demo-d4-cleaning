"""B2 офлайн-контракт. Реальный OAuth/транспорт закрыт до разрешённого SDK-этапа."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import re
import sqlite3
import time
from urllib.parse import quote
import uuid

import httpx

from .model import Failure, Lead
from .sheets_config import ConfigurationError, MAX_ROW, SheetsConfig
from .telegram import no_http_logs

HEADERS = ['submission_id', 'accepted_at_utc', 'sofa', 'size', 'material', 'description',
           'synthetic_channel', 'synthetic_contact', 'photo_count']
_ORIGIN = 'https://sheets.googleapis.com'
_TIMEOUT = httpx.Timeout(connect=3, write=5, read=5, pool=1)


@dataclass(frozen=True, repr=False)
class OfflineAuthorization:
    """Только искусственный поставщик, не имитация успешного настоящего OAuth."""
    account: str
    access_token: str = field(repr=False)
    expires_at: float
    clock: object = time.time

    def token(self):
        if (not math.isfinite(self.expires_at) or self.expires_at - self.clock() < 60
                or not re.fullmatch(r'[A-Za-z0-9._~+/=-]{1,8192}', self.access_token)):
            raise ConfigurationError('Искусственная авторизация Sheets недоступна или истекла.')
        return self.access_token

    def __repr__(self):
        return 'OfflineAuthorization(synthetic=True)'


@dataclass(frozen=True)
class SheetsOutcome:
    kind: str
    reason: str
    delay: int = 0


class _Stop(Exception):
    def __init__(self, kind, reason, delay=0):
        self.outcome = SheetsOutcome(kind, reason, delay)


def row_for_submission(store, sid, connection):
    try:
        if str(uuid.UUID(sid)) != sid:
            raise ValueError()
    except (ValueError, AttributeError):
        raise Failure(422, 'Нужен один корректный submission_id.') from None
    row = connection.execute('SELECT * FROM submissions WHERE id=?', (sid,)).fetchone()
    now = store.settings.clock()
    if (not row or not isinstance(row['created'], (int, float)) or not math.isfinite(row['created'])
            or not 0 <= now - row['created'] < store.settings.retention):
        raise Failure(410, 'Заявка отсутствует или срок её хранения истёк.')
    try:
        lead = Lead.model_validate_json(row['payload'])
        count = connection.execute('SELECT count(*) FROM photos WHERE submission_id=?', (sid,)).fetchone()[0]
        if not 1 <= count <= 3:
            raise ValueError()
        accepted = datetime.fromtimestamp(row['created'], timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    except Exception:
        raise Failure(422, 'Сохранённая заявка не соответствует синтетической схеме Sheets.') from None
    # Все 9 значений — строки RAW. CSV-экранирование здесь не применяется.
    return [sid, accepted, lead.sofa, lead.size, lead.material, lead.description,
            lead.channel, lead.contact, str(count)]


class SheetsAdapter:
    def __init__(self, config: SheetsConfig, authorization, *, transport):
        if (not isinstance(transport, httpx.MockTransport) or not isinstance(authorization, OfflineAuthorization)
                or not config.spreadsheet_id or authorization.account != config.service_account):
            raise ConfigurationError('B2 требует явные искусственную авторизацию и MockTransport согласованного ресурса.')
        self.config = config
        self.authorization = authorization
        self._transport = transport
        self.offline = True

    def __repr__(self):
        return 'SheetsAdapter(offline=True)'

    def _request(self, client, method, cell_range, *, body=None, append=False):
        # Даже bearer/SDK-исключения могут включать закодированные секреты: не возвращать их текст.
        try:
            token = self.authorization.token()
        except Exception:
            raise _Stop('failed', 'authorization_unavailable') from None
        path = '/v4/spreadsheets/' + self.config.spreadsheet_id + '/values/' + quote(cell_range, safe='')
        params = {'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'} if append else {
            'majorDimension': 'ROWS', 'valueRenderOption': 'UNFORMATTED_VALUE'}
        try:
            deadline = time.monotonic() + 15
            with client.stream(method, _ORIGIN + path + (':append' if append else ''), params=params,
                               headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json',
                                        'Accept-Encoding': 'identity'}, json=body) as response:
                status = response.status_code
                if status in {400, 401, 403, 404}:
                    raise _Stop('failed', 'rejected')
                if status == 429:
                    retry = response.headers.get('Retry-After', '')
                    if retry.isascii() and retry.isdigit() and 1 <= int(retry) <= 3600:
                        raise _Stop('pending', 'rate_limit', int(retry))
                if status != 200 or response.is_redirect:
                    raise _Stop('unknown' if append else 'pending', 'unconfirmed_http', 30)
                if response.headers.get('content-encoding', 'identity').lower() not in {'identity', ''}:
                    raise _Stop('unknown' if append else 'failed', 'unexpected_encoding')
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > 2 * 1024 * 1024 or time.monotonic() > deadline:
                        raise _Stop('unknown' if append else 'failed', 'response_limit')
                    raw.extend(chunk)
                try:
                    def invalid_constant(value):
                        raise ValueError()
                    data = json.loads(raw.decode('utf-8'), parse_constant=invalid_constant)
                    if not isinstance(data, dict):
                        raise ValueError()
                    return data
                except (ValueError, UnicodeError):
                    raise _Stop('unknown' if append else 'failed', 'invalid_response') from None
        except _Stop:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise _Stop('pending', 'not_transmitted', 30) from None
        except Exception:
            raise _Stop('unknown' if append else 'pending', 'uncertain_transport', 30) from None

    @staticmethod
    def _range(value, start, end):
        return value in {f'Leads!A{start}:I{end}', f"'Leads'!A{start}:I{end}"}

    def _read(self, client, start, end):
        data = self._request(client, 'GET', f'Leads!A{start}:I{end}')
        values = data.get('values', [])
        if (not self._range(data.get('range'), start, end) or data.get('majorDimension') != 'ROWS'
                or not isinstance(values, list) or len(values) > end - start + 1):
            raise _Stop('failed', 'invalid_read_range')
        rows = []
        for row in values:
            if (not isinstance(row, list) or len(row) > 9
                    or any(type(v) not in {str, int, float, bool} or (isinstance(v, str) and len(v) > 2000)
                           for v in row)):
                raise _Stop('failed', 'invalid_read_values')
            rows.append(row + [''] * (9 - len(row)))
        return rows

    def execute(self, row, *, reconcile=False):
        """Один ограниченный цикл. Reconcile ни при каких исходах не делает POST."""
        wrote = False
        append_accepted = False
        with no_http_logs():
            try:
                with httpx.Client(transport=self._transport, timeout=_TIMEOUT, trust_env=False,
                                  follow_redirects=False) as client:
                    if self._read(client, 1, 1) != [HEADERS]:
                        raise _Stop('failed', 'header_mismatch')
                    rows = self._read(client, 2, MAX_ROW)
                    matches = [r for r in rows if r[0] == row[0]]
                    if matches:
                        if len(matches) == 1 and matches[0] == row:
                            return SheetsOutcome('delivered', 'row_confirmed')
                        raise _Stop('failed', 'duplicate_or_conflicting_uuid')
                    if reconcile:
                        return SheetsOutcome('unknown', 'row_not_found')
                    # Выделенный лист заполняется подряд. Дыры/чужая структура — разбор владельцем, не перестройка.
                    if len(rows) >= MAX_ROW - 1 or any(not r[0] for r in rows):
                        raise _Stop('failed', 'capacity_or_layout')
                    target = len(rows) + 2
                    wrote = True
                    data = self._request(client, 'POST', f'Leads!A1:I{MAX_ROW}',
                                         body={'majorDimension': 'ROWS', 'values': [row]}, append=True)
                    append_accepted = True
                    updates = data.get('updates')
                    if (data.get('spreadsheetId') != self.config.spreadsheet_id or not isinstance(updates, dict)
                            or updates.get('spreadsheetId') != self.config.spreadsheet_id
                            or not self._range(updates.get('updatedRange'), target, target)
                            or any(type(updates.get(key)) is not int or updates[key] != value for key, value in
                                   {'updatedRows': 1, 'updatedColumns': 9, 'updatedCells': 9}.items())):
                        raise _Stop('unknown', 'invalid_append_ack')
                    if self._read(client, target, target) != [row]:
                        raise _Stop('unknown', 'readback_mismatch')
                    return SheetsOutcome('delivered', 'row_confirmed')
            except _Stop as stop:
                if reconcile or append_accepted or (wrote and stop.outcome.reason not in {
                        'rejected', 'rate_limit', 'not_transmitted', 'authorization_unavailable'}):
                    return SheetsOutcome('unknown', stop.outcome.reason)
                return stop.outcome
            except Exception:
                return SheetsOutcome('unknown' if wrote or reconcile else 'failed', 'unconfirmed_operation')


def send_one(store, sid, adapter, *, reconcile=False):
    """Вызывающий локальный CLI обязан держать прежний registry flock."""
    if not isinstance(adapter, SheetsAdapter):
        raise ConfigurationError('Нужен явный офлайн-адаптер Sheets.')
    mode = 'mock' if adapter.offline else 'live'  # live ветка проверяется только искусственными unit fixtures
    simulated = int(adapter.offline)
    now = store.settings.clock()
    with store.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        row = row_for_submission(store, sid, c)
        job = c.execute("SELECT * FROM deliveries WHERE submission_id=? AND channel='sheets'", (sid,)).fetchone()
        if not job:
            raise Failure(404, 'Задание Sheets отсутствует.')
        try:
            meta = json.loads(job['error'] or '{}')
        except (ValueError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if job['status'] == 'not_connected' and not job['simulated']:
            if reconcile:
                raise Failure(409, 'Сверка разрешена только для неизвестного результата Sheets.')
        elif job['status'].startswith(mode + '_') and job['simulated'] == simulated:
            if meta.get('binding') != adapter.config.binding:
                raise Failure(409, 'Ресурс или техническая учётная запись Sheets изменились.')
            if job['status'] == mode + '_processing':
                c.execute("UPDATE deliveries SET status=?,error=? WHERE submission_id=? AND channel='sheets'",
                          (mode + '_unknown', json.dumps(meta | {'reason': 'interrupted'}), sid))
                if not reconcile:
                    return SheetsOutcome('unknown', 'interrupted')
            elif job['status'] == mode + '_delivered':
                return SheetsOutcome('delivered', 'already_confirmed')
            elif reconcile:
                if job['status'] != mode + '_unknown':
                    raise Failure(409, 'Сверка разрешена только для неизвестного результата Sheets.')
            elif job['status'] != mode + '_pending':
                raise Failure(409, 'Повтор записи Sheets запрещён. Для unknown используйте только сверку.')
            if not reconcile and job['next_at'] > now:
                raise Failure(429, 'Следующая ручная попытка Sheets ещё не разрешена.')
        else:
            raise Failure(409, 'Тестовое и живое задания Sheets не преобразуются друг в друга.')
        if reconcile:
            checks = meta.get('checks', 0)
            if type(checks) is not int or not 0 <= checks < 3:
                raise Failure(409, 'Лимит адресных сверок Sheets исчерпан. Нужен разбор владельцем.')
            meta = meta | {'checks': checks + 1, 'reason': 'reconciling'}
            # При аварии read-only сверки сохраняется unknown; право на append не появляется.
            c.execute("UPDATE deliveries SET status=?,error=? WHERE submission_id=? AND channel='sheets'",
                      (mode + '_unknown', json.dumps(meta), sid))
        else:
            if not 0 <= job['attempts'] < 3:
                raise Failure(409, 'Лимит попыток Sheets исчерпан.')
            meta = {'sheets_v': 1, 'binding': adapter.config.binding, 'reason': 'processing', 'checks': 0}
            c.execute("UPDATE deliveries SET status=?,simulated=?,attempts=attempts+1,error=? WHERE submission_id=? AND channel='sheets'",
                      (mode + '_processing', simulated, json.dumps(meta), sid))
    try:
        outcome = adapter.execute(row, reconcile=reconcile)
    except Exception:
        outcome = SheetsOutcome('unknown', 'unconfirmed_operation')
    state = outcome.kind
    if state == 'pending' and job['attempts'] + 1 >= 3:
        state = 'failed'
    meta['reason'] = outcome.reason
    try:
        with store.connection() as c:
            updated = c.execute("UPDATE deliveries SET status=?,next_at=?,error=? WHERE submission_id=? AND channel='sheets' AND status=? AND simulated=?",
                                (mode + '_' + state, now + outcome.delay, json.dumps(meta), sid,
                                 mode + ('_unknown' if reconcile else '_processing'), simulated))
            if updated.rowcount != 1:
                return SheetsOutcome('unknown', 'state_conflict')
    except (OSError, sqlite3.Error):
        return SheetsOutcome('unknown', 'storage_after_operation')
    return SheetsOutcome(state, outcome.reason, outcome.delay)


def build_live_adapter(config):
    raise ConfigurationError('Live Sheets закрыт до реализации и приёмки официального SDK. Отправки нет.')
