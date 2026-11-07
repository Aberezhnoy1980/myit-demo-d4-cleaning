"""Одна адресная sendPhoto. По умолчанию нет сети, секретов и автоматических отправок."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
import json
import logging
import math
import sqlite3
import threading
import time
import uuid

import httpx
from PIL import Image

from .model import Failure, Lead, MIB
from .telegram_config import ConfigurationError, TelegramConfig

_LOG_LOCK = threading.RLock()
_TIMEOUT = httpx.Timeout(connect=3, write=5, read=5, pool=1)
_ORIGIN = 'https://api.telegram.org'


@contextmanager
def no_http_logs():
    # Token — часть URL. CLI монопольно владеет registry lock; HTTPX/httpcore
    # request/response/exception logs исключены, а не сохраняются с URL.
    # RLock также защищает восстановление logging.disable при потоковых тестах.
    with _LOG_LOCK:
        previous = logging.root.manager.disable
        logging.disable(max(previous, logging.CRITICAL))
        try:
            yield
        finally:
            logging.disable(previous)


@dataclass(frozen=True)
class TelegramOutcome:
    kind: str  # delivered/retry/failed/unknown
    reason: str
    delay: int = 0
    message_id: int | None = None


@dataclass(frozen=True)
class PhotoRequest:
    submission_id: str
    photo: bytes = field(repr=False)
    caption: str = field(repr=False)


class TelegramAdapter:
    def __init__(self, config: TelegramConfig, *, transport=None, allow_live=False):
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise ConfigurationError('Для офлайн-проверки допустим только MockTransport.')
        if not config.token or not config.chat_id:
            raise ConfigurationError('Авторизация Telegram не настроена.')
        self.offline = transport is not None
        if not self.offline and (not allow_live or not config.enabled):
            raise ConfigurationError('Живая отправка Telegram выключена.')
        self.config = config
        self._transport = transport

    def __repr__(self):
        return f'TelegramAdapter(offline={self.offline})'

    def send(self, request: PhotoRequest):
        # Не возвращаем Request/Response/Exception или description Telegram наружу.
        with no_http_logs():
            try:
                transport = self._transport or httpx.HTTPTransport(retries=0, trust_env=False)
                with httpx.Client(transport=transport, timeout=_TIMEOUT, trust_env=False,
                                  follow_redirects=False) as client:
                    deadline = time.monotonic() + 15
                    with client.stream('POST', _ORIGIN + '/bot' + self.config.token + '/sendPhoto',
                                       data={'chat_id': self.config.chat_id, 'caption': request.caption},
                                       files={'photo': ('d4-demo.jpg', request.photo, 'image/jpeg')},
                                       headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'}) as response:
                        if response.is_redirect:
                            return TelegramOutcome('unknown', 'redirect_refused')
                        if response.headers.get('content-encoding', 'identity').lower() not in {'identity', ''}:
                            return TelegramOutcome('unknown', 'unexpected_encoding')
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            if len(body) + len(chunk) > 65536 or time.monotonic() > deadline:
                                return TelegramOutcome('unknown', 'response_limit')
                            body.extend(chunk)
                        try:
                            data = json.loads(body.decode('utf-8'))
                        except (ValueError, UnicodeError):
                            return TelegramOutcome('unknown', 'invalid_json')
                        return self._interpret(response.status_code, data)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                return TelegramOutcome('retry', 'not_transmitted', 30)
            except Exception:
                # Даже repr исключения содержит URL с token. Не сохраняем его
                # и не поднимаем заново через exception chaining.
                return TelegramOutcome('unknown', 'uncertain_transport')

    def _interpret(self, status, data):
        if not isinstance(data, dict):
            return TelegramOutcome('unknown', 'invalid_response')
        if status == 200 and data.get('ok') is True:
            result = data.get('result')
            if isinstance(result, dict):
                chat = result.get('chat')
                mid = result.get('message_id')
                photos = result.get('photo')
                valid_photos = isinstance(photos, list) and bool(photos) and all(
                    isinstance(photo, dict) and isinstance(photo.get('file_id'), str) and bool(photo['file_id'])
                    and type(photo.get('width')) is int and photo['width'] > 0
                    and type(photo.get('height')) is int and photo['height'] > 0 for photo in photos)
                if (isinstance(chat, dict) and type(chat.get('id')) is int
                        and chat['id'] == int(self.config.chat_id) and type(mid) is int and mid > 0
                        and valid_photos):
                    return TelegramOutcome('delivered', 'message_created', message_id=mid)
            return TelegramOutcome('unknown', 'unconfirmed_recipient_or_message')
        if data.get('ok') is False and type(data.get('error_code')) is int and data['error_code'] == status:
            if status in {400, 401, 403, 404}:
                return TelegramOutcome('failed', 'rejected')
            if status == 429:
                parameters = data.get('parameters')
                retry = parameters.get('retry_after') if isinstance(parameters, dict) else None
                if type(retry) is int and 1 <= retry <= 3600:
                    return TelegramOutcome('retry', 'rate_limit', retry)
                return TelegramOutcome('unknown', 'invalid_retry_after')
        return TelegramOutcome('unknown', 'unconfirmed_response')


def prepare_photo(store, submission_id, connection):
    try:
        if str(uuid.UUID(submission_id)) != submission_id:
            raise ValueError()
    except (ValueError, AttributeError):
        raise Failure(422, 'Нужен один корректный submission_id.') from None
    row = connection.execute('SELECT payload,created FROM submissions WHERE id=?', (submission_id,)).fetchone()
    now = store.settings.clock()
    if (not row or not isinstance(row['created'], (float, int)) or not math.isfinite(row['created'])
            or not 0 <= now - row['created'] < store.settings.retention):
        raise Failure(410, 'Заявка отсутствует или срок её хранения истёк.')
    try:
        lead = Lead.model_validate_json(row['payload'])
        photo_row = connection.execute('SELECT content FROM photos WHERE submission_id=? ORDER BY rowid LIMIT 1', (submission_id,)).fetchone()
        photo = photo_row[0] if photo_row else b''
        if not isinstance(photo, bytes) or not 0 < len(photo) <= 5 * MIB:
            raise ValueError()
        with Image.open(BytesIO(photo), formats=['JPEG']) as image:
            if image.width > 2000 or image.height > 2000 or image.getexif() or 'icc_profile' in image.info:
                raise ValueError()
            image.verify()
    except Exception:
        raise Failure(422, 'Данные или подготовленный JPEG не прошли проверку. Отправки нет.') from None
    caption = (f'Демо D4 · запрос {submission_id}\n'
               f'Диван: {lead.sofa} · {lead.size}\nОбивка: {lead.material}\n'
               'Синтетическая заявка. Услуги не оказываются.')
    return PhotoRequest(submission_id, photo, caption)


def send_one(store, submission_id, adapter):
    """Один адресный claim и один вызов адаптера. Вызывающий CLI держит registry flock."""
    if not isinstance(adapter, TelegramAdapter):
        raise ConfigurationError('Нужен явный Telegram-адаптер.')
    mode = 'mock' if adapter.offline else 'live'
    simulated = int(adapter.offline)
    now = store.settings.clock()
    with store.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        request = prepare_photo(store, submission_id, c)
        job = c.execute("SELECT * FROM deliveries WHERE submission_id=? AND channel='telegram'", (submission_id,)).fetchone()
        if not job:
            raise Failure(404, 'Задание Telegram не найдено.')
        try:
            meta = json.loads(job['error'] or '{}')
        except (ValueError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if job['status'] == 'not_connected' and job['simulated'] == 0:
            pass
        elif job['status'].startswith(mode + '_') and job['simulated'] == simulated:
            if meta.get('binding') != adapter.config.binding:
                raise Failure(409, 'Получатель или бот отличаются от первого запуска этого задания.')
            if job['status'] == mode + '_processing':
                meta['reason'] = 'interrupted'
                c.execute("UPDATE deliveries SET status=?,error=? WHERE submission_id=? AND channel='telegram'",
                          (mode + '_unknown', json.dumps(meta), submission_id))
                return TelegramOutcome('unknown', 'interrupted')
            if job['status'] == mode + '_delivered':
                return TelegramOutcome('delivered', 'already_created')
            if job['status'] != mode + '_pending' or job['attempts'] >= 3:
                raise Failure(409, 'Повтор запрещён: результат неизвестен, отказ окончателен или попытки исчерпаны.')
            if job['next_at'] > now:
                raise Failure(429, 'Срок следующей разрешённой попытки ещё не наступил.', retry_after=max(1, math.ceil(job['next_at'] - now)))
        else:
            raise Failure(409, 'Тестовое и живое задания не преобразуются друг в друга. Нужна новая согласованная заявка.')
        if not 0 <= job['attempts'] < 3:
            raise Failure(409, 'Лимит попыток Telegram исчерпан.')
        attempts = job['attempts'] + 1
        meta = {'telegram_v': 1, 'binding': adapter.config.binding, 'reason': 'processing'}
        c.execute("UPDATE deliveries SET status=?,simulated=?,attempts=?,error=? WHERE submission_id=? AND channel='telegram'",
                  (mode + '_processing', simulated, attempts, json.dumps(meta), submission_id))
    try:
        outcome = adapter.send(request)
    except Exception:
        outcome = TelegramOutcome('unknown', 'uncertain_transport')
    state = outcome.kind
    if state == 'retry':
        state = 'pending' if attempts < 3 else 'failed'
    meta['reason'] = outcome.reason
    if outcome.message_id is not None:
        meta['message_id'] = outcome.message_id  # приватная служебная квитанция, не клиентский ответ
    try:
        with store.connection() as c:
            updated = c.execute("UPDATE deliveries SET status=?,next_at=?,error=? WHERE submission_id=? AND channel='telegram' AND status=? AND simulated=?",
                      (mode + '_' + state, now + outcome.delay, json.dumps(meta), submission_id, mode + '_processing', simulated))
            if updated.rowcount != 1:
                return TelegramOutcome('unknown', 'state_conflict')
    except (OSError, sqlite3.Error):
        return TelegramOutcome('unknown', 'storage_after_send')
    return TelegramOutcome(state, outcome.reason, outcome.delay)
