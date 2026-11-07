import asyncio
from contextlib import asynccontextmanager
import fcntl
import re
import sqlite3

from fastapi import FastAPI, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

from .model import Failure, Lead, MIB, ROOT, Settings
from .photos import decode_photo
from .store import Store, fingerprint


def fail_response(error):
    headers = {'Retry-After': str(error.retry_after)} if error.retry_after else {}
    return JSONResponse({'message': error.message, 'fields': error.fields}, status_code=error.status, headers=headers)


class Security:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode('latin1')
        if not re.fullmatch(r'(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?', host):
            return await fail_response(Failure(400, 'Разрешён только локальный адрес.'))(scope, receive, send)
        origin = headers.get(b'origin', b'').decode('latin1')
        if origin and origin != f"{scope.get('scheme', 'http')}://{host}":
            return await fail_response(Failure(403, 'Откройте форму на локальном адресе стенда.'))(scope, receive, send)
        if headers.get(b'sec-fetch-site') == b'cross-site':
            return await fail_response(Failure(403, 'Межсайтовый запрос запрещён.'))(scope, receive, send)

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] += [
                    (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                    (b'referrer-policy', b'no-referrer'),
                    (b'content-security-policy', b"default-src 'self'; img-src 'self' blob:; connect-src 'self'; script-src 'self'; style-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"),
                ]
            await send(message)
        await self.app(scope, receive, secure_send)


async def bounded_form(request):
    if not request.headers.get('content-type', '').lower().startswith('multipart/form-data;'):
        raise Failure(415, 'Нужна форма с фотографиями.')
    length = request.headers.get('content-length')
    if length:
        try:
            if not 0 <= int(length) <= 13 * MIB:
                raise Failure(413, 'Запрос слишком большой. Фото: до 12 МБ вместе.')
        except ValueError:
            raise Failure(400, 'Не удалось прочитать запрос.') from None
    chunks, total = [], 0
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                total += len(chunk)
                if total > 13 * MIB:
                    raise Failure(413, 'Запрос слишком большой. Фото: до 12 МБ вместе.')
                chunks.append(chunk)
    except (TimeoutError, ClientDisconnect):
        raise Failure(408, 'Передача прервана. Повторите тот же запрос; повтор не создаёт новую заявку.') from None
    body = b''.join(chunks)
    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}
    parsed = Request(request.scope, receive=receive)
    return await parsed.form(max_files=3, max_fields=6, max_part_size=8192)


def create_app(settings=None):
    settings = settings or Settings()
    store = Store(settings)

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (settings.data_dir / '.server.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            store.initialize()
            store.cleanup(apply=True)
            async def cleanup_loop():
                while True:
                    await asyncio.sleep(settings.cleanup_interval)
                    try:
                        await run_in_threadpool(store.cleanup, True)
                    except (sqlite3.Error, OSError):
                        pass  # повтор на следующем цикле, без данных/tracebacks в логах
            task = asyncio.create_task(cleanup_loop())
            try:
                yield
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    site = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    slots = asyncio.Semaphore(2)

    @site.exception_handler(HTTPException)
    async def http_error(request, error):
        return fail_response(Failure(error.status_code, 'Не удалось прочитать запрос или ресурс недоступен.'))

    @site.get('/api/health')
    async def health():
        return {'mode': 'local-synthetic', 'storage': 'sqlite', 'delivery': 'not_connected'}

    @site.post('/api/submissions')
    async def submit(request: Request):
        acquired = False
        try:
            key = request.headers.get('idempotency-key', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', key):
                raise Failure(400, 'Запросу нужен корректный ключ повтора.')
            try:
                async with asyncio.timeout(0.1):
                    await slots.acquire()
                    acquired = True
            except TimeoutError:
                raise Failure(429, 'Стенд занят. Повторите запрос чуть позже.', retry_after=5) from None
            await run_in_threadpool(store.rate)
            form = await bounded_form(request)
            try:
                fields, originals = {}, []
                for name, value in form.multi_items():
                    if isinstance(value, UploadFile):
                        if name != 'photos':
                            raise Failure(422, 'Неизвестное поле файла.')
                        content = await value.read(5 * MIB + 1)
                        if not content or len(content) > 5 * MIB:
                            raise Failure(422, 'Проверьте фотографии.', {'photos': 'Каждое фото должно быть непустым и не больше 5 МБ.'})
                        originals.append((content, value.content_type))
                    else:
                        if name in fields or name not in Lead.model_fields:
                            raise Failure(422, 'Повторяющееся или неизвестное поле.')
                        fields[name] = value
                try:
                    lead = Lead.model_validate(fields)
                except ValidationError:
                    raise Failure(422, 'Проверьте поля. Нужны параметры дивана, описание до 1000 символов и вымышленный контакт.',
                                  {'contact': 'Только адрес @example.test или Telegram @demo_…; проверьте остальные поля.'}) from None
                if not 1 <= len(originals) <= 3 or sum(len(x[0]) for x in originals) > 12 * MIB:
                    raise Failure(422, 'Проверьте фотографии.', {'photos': 'Нужны 1–3 фото, до 12 МБ вместе.'})
                body_hash = fingerprint(lead, originals)
                replay = await run_in_threadpool(store.replay, key, body_hash)
                if replay:
                    return JSONResponse(replay)
                clean = []
                for content, mime in originals:
                    clean.append(await run_in_threadpool(decode_photo, content, mime))
                accepted = await run_in_threadpool(store.save, key, body_hash, lead, clean)
                return JSONResponse(accepted, status_code=200 if accepted['replayed'] else 201)
            finally:
                await form.close()
        except Failure as error:
            return fail_response(error)
        except (sqlite3.Error, OSError):
            return fail_response(Failure(503, 'Хранилище сейчас недоступно. Повторите тот же запрос: если он уже сохранён, дубль не появится.'))
        finally:
            if acquired:
                slots.release()

    @site.get('/api/receipts/{sid}')
    async def receipt(sid: str, request: Request):
        try:
            if not re.fullmatch(r'[0-9a-f-]{36}', sid):
                raise Failure(404, 'Квитанция недоступна.')
            return await run_in_threadpool(store.receipt, sid, request.headers.get('x-receipt-key', ''))
        except Failure as error:
            return fail_response(error)
        except (sqlite3.Error, OSError):
            return fail_response(Failure(503, 'Квитанция временно недоступна. Это не подтверждение доставки.'))

    site.mount('/', StaticFiles(directory=ROOT / 'public', html=True), name='public')
    app.mount(settings.prefix or '/', site)
    app.add_middleware(Security)
    return app
