from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Lock
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
import time
import uuid

from .model import Failure
from .delivery import channel_message, delivery_message


SCHEMA = '''
CREATE TABLE IF NOT EXISTS submissions(
    id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
    body_hash TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
    receipt_key TEXT NOT NULL, receipt_expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS photos(
    name TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    content BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(
    submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    channel TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'not_connected',
    simulated INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0, error TEXT,
    PRIMARY KEY(submission_id, channel));
CREATE TABLE IF NOT EXISTS rate_events(stamp REAL NOT NULL);
CREATE INDEX IF NOT EXISTS rate_stamp ON rate_events(stamp);
'''


def schema_signature(connection):
    # Строгий формат backup D4: DDL включает колонки, NOT NULL, PK/UNIQUE,
    # defaults, FK/CASCADE, индексы; посторонние триггеры/таблицы также отвергаются.
    return [tuple(' '.join(value.split()) if isinstance(value, str) else value for value in row)
            for row in connection.execute(
                'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name')]


def validate_restore_snapshot(connection):
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('Invalid backup integrity')
    with closing(sqlite3.connect(':memory:')) as reference:
        reference.executescript(SCHEMA)
        if schema_signature(connection) != schema_signature(reference):
            raise ValueError('Incompatible D4 backup schema')
        for pragma in ('user_version', 'application_id', 'encoding', 'page_size'):
            if connection.execute('PRAGMA ' + pragma).fetchone() != reference.execute('PRAGMA ' + pragma).fetchone():
                raise ValueError('Incompatible D4 backup format')
    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('Invalid backup references')


class Store:
    def __init__(self, settings, *, live_limit=None):
        self.settings = settings
        self.path = settings.data_dir / 'registry.sqlite3'
        self._live_remaining = live_limit
        self._save_lock = Lock() if live_limit is not None else nullcontext()

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=2)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA secure_delete=ON')
        try:
            with c:
                yield c
        finally:
            c.close()

    def initialize(self):
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.settings.data_dir, 0o700)
        if self.path.is_symlink():
            raise ValueError('Database symlink is forbidden')
        with self.connection() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA synchronous=FULL')
            c.executescript(SCHEMA)
            # Неопределённость после остановки воркера: никогда не повторять автоматически.
            c.execute("UPDATE deliveries SET status='unknown',error='interrupted' WHERE status='processing'")
        os.chmod(self.path, 0o600)

    def rate(self):
        now = self.settings.clock()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM rate_events WHERE stamp<=?', (now - 3600,))
            minute = c.execute('SELECT count(*) FROM rate_events WHERE stamp>?', (now - 60,)).fetchone()[0]
            hour = c.execute('SELECT count(*) FROM rate_events').fetchone()[0]
            if minute >= self.settings.minute_limit or hour >= self.settings.hour_limit:
                raise Failure(429, 'Слишком много запросов. Данные формы остаются у вас; повторите позже.', retry_after=60)
            c.execute('INSERT INTO rate_events VALUES(?)', (now,))

    def replay(self, key, body_hash):
        with self.connection() as c:
            row = c.execute('SELECT * FROM submissions WHERE request_key=?', (key,)).fetchone()
            if not row:
                return None
            if not hmac.compare_digest(row['body_hash'], body_hash):
                raise Failure(409, 'Этот повтор относится к другому запросу. Вернитесь к форме и создайте новый запрос.')
            return self.acceptance(row, replay=True, channels=self.delivery_rows(c, row['id']))

    @staticmethod
    def delivery_rows(connection, sid):
        rows = connection.execute('SELECT channel,status,simulated FROM deliveries WHERE submission_id=? ORDER BY channel', (sid,)).fetchall()
        return [dict(row) | {
            'mode': 'test' if row['simulated'] else ('live' if row['status'].startswith('live_') else 'not_connected'),
            'message': channel_message(row),
        } for row in rows]

    def settle_linked_delivery(self, sid=None, channel=None):
        """Под блокировкой реестра завершает прерванные задания формы."""
        with self.connection() as c:
            c.execute("""
                UPDATE deliveries
                SET status = CASE status WHEN 'mock_queued' THEN 'mock_failed'
                    WHEN 'mock_processing' THEN 'mock_unknown'
                    WHEN 'live_queued' THEN 'live_failed' ELSE 'live_unknown' END
                WHERE ((simulated = 1 AND status IN ('mock_queued', 'mock_processing'))
                    OR (simulated = 0 AND (status = 'live_queued' OR (status = 'live_processing'
                        AND CASE WHEN json_valid(error) THEN json_extract(error, '$.linked') END = 1))))
                  AND (? IS NULL OR submission_id = ?) AND (? IS NULL OR channel = ?)
            """, (sid, sid, channel, channel))

    def acceptance(self, row, replay=False, channels=None):
        channels = channels or []
        return {'submission_id': row['id'], 'receipt_key': row['receipt_key'],
                'receipt_expires': row['receipt_expires'], 'replayed': replay,
                'message': delivery_message(channels), 'channels': channels}

    def save(self, key, body_hash, lead, photos, *, test_delivery=False):
        now = self.settings.clock()
        with self._save_lock, self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM submissions WHERE request_key=?', (key,)).fetchone()
            if row:
                if row['body_hash'] != body_hash:
                    raise Failure(409, 'Ключ повтора уже использован с другими данными.')
                return self.acceptance(row, replay=True, channels=self.delivery_rows(c, row['id']))
            live = self._live_remaining is not None
            if live and self._live_remaining == 0:
                raise Failure(409, 'Лимит новых запросов закрытого прогона исчерпан.')
            count = c.execute('SELECT count(*) FROM submissions').fetchone()[0]
            used = c.execute('SELECT coalesce(sum(length(content)),0) FROM photos').fetchone()[0]
            if count >= self.settings.record_limit or used + sum(map(len, photos)) > self.settings.storage_limit:
                raise Failure(507, 'Демо-реестр заполнен. Запрос не сохранён. Попробуйте после очистки стенда.')
            sid = str(uuid.uuid4())
            receipt_key = secrets.token_urlsafe(32)
            c.execute('INSERT INTO submissions VALUES(?,?,?,?,?,?,?)',
                      (sid, key, body_hash, lead.model_dump_json(), now, receipt_key, now + self.settings.receipt_ttl))
            c.executemany('INSERT INTO photos VALUES(?,?,?)',
                          [(uuid.uuid4().hex + '.jpg', sid, p) for p in photos])
            status = 'mock_queued' if test_delivery else 'not_connected'
            if live:
                status = 'live_queued'
            c.executemany(
                'INSERT INTO deliveries(submission_id,channel,status,simulated) VALUES(?,?,?,?)',
                [(sid, channel, status, int(test_delivery)) for channel in ('telegram', 'sheets')],
            )
            accepted = self.acceptance(c.execute('SELECT * FROM submissions WHERE id=?', (sid,)).fetchone(),
                                       channels=self.delivery_rows(c, sid))
            if live:
                # Квота переживает очистку заявок и расходуется только после commit.
                c.commit()
                self._live_remaining -= 1
            return accepted

    def receipt(self, sid, key):
        with self.connection() as c:
            row = c.execute('SELECT * FROM submissions WHERE id=?', (sid,)).fetchone()
            if not row or not key or not hmac.compare_digest(row['receipt_key'], key) or row['receipt_expires'] <= self.settings.clock():
                raise Failure(404, 'Квитанция недоступна или срок её действия истёк.')
            jobs = self.delivery_rows(c, sid)
            return {'submission_id': sid, 'created': row['created'], 'receipt_expires': row['receipt_expires'],
                    'photos': c.execute('SELECT count(*) FROM photos WHERE submission_id=?', (sid,)).fetchone()[0],
                    'message': delivery_message(jobs),
                    'channels': jobs}

    def cleanup(self, apply=False):
        cutoff = self.settings.clock() - self.settings.retention
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            count = c.execute('SELECT count(*) FROM submissions WHERE created<=?', (cutoff,)).fetchone()[0]
            if apply:
                c.execute('DELETE FROM submissions WHERE created<=?', (cutoff,))
                c.execute('DELETE FROM rate_events WHERE stamp<=?', (self.settings.clock() - 3600,))
        if apply:
            with self.connection() as c:
                c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return count

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists() or destination.is_symlink():
            raise ValueError('Backup destination must be new')
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with self.connection() as c, sqlite3.connect(destination) as backup:
                c.backup(backup)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def restore(self, source):
        # Вызывающий CLI сначала берёт тот же эксклюзивный lock, что сервер.
        source = Path(source)
        if source.is_symlink() or not source.is_file() or not self.path.is_file() or self.path.is_symlink():
            raise ValueError('Restore requires private files and an existing registry')
        if source.samefile(self.path):
            raise ValueError('Backup must differ from current registry')
        # Рабочая база до публикации не открывается для записи. Снимок изолирован
        # от изменения исходного файла; все операции initialize/cleanup — на копии.
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=self.settings.data_dir,
                                         ignore_cleanup_errors=True) as directory:
            prepared = Store(replace(self.settings, data_dir=Path(directory)))
            with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as backup:
                with closing(sqlite3.connect(prepared.path)) as staged:
                    backup.backup(staged)
                    validate_restore_snapshot(staged)
            prepared.initialize()
            prepared.cleanup(apply=True)
            with closing(sqlite3.connect(prepared.path)) as staged:
                validate_restore_snapshot(staged)
                deadline = time.monotonic() + 10

                def progress(status, remaining, total):
                    # SQLITE_DONE уже означает commit: после него не поднимаем
                    # исключения. До DONE SQLite откатывает частичную копию при отказе.
                    if status != sqlite3.SQLITE_DONE and time.monotonic() > deadline:
                        raise TimeoutError('Restore copy timed out')

                with self.connection() as target:
                    staged.backup(target, pages=16, progress=progress, sleep=0.05)
        # После commit нет initialize/cleanup или иных изменений рабочего реестра.


def fingerprint(lead, originals):
    value = [lead.model_dump(), [(mime, hashlib.sha256(content).hexdigest()) for content, mime in originals]]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
