"""Только тестовые адаптеры. Модуль не содержит HTTP-клиента/URL/credentials."""
from dataclasses import dataclass


@dataclass
class Outcome:
    kind: str  # success, retry, failed, unknown
    reason: str = ''
    delay: int = 30


class TestAdapter:
    __test__ = False

    def __init__(self, scenario):
        if scenario not in {'success', 'unavailable', 'rate_limit', 'timeout', 'forbidden'}:
            raise ValueError('Unknown test scenario')
        self.scenario = scenario

    def send(self, submission_id):
        return {'success': Outcome('success'), 'unavailable': Outcome('retry', 'unavailable'),
                'rate_limit': Outcome('retry', 'rate_limit', 60),
                'timeout': Outcome('unknown', 'timeout'), 'forbidden': Outcome('failed', 'forbidden')}[self.scenario]


def arm_test_jobs(store):
    with store.connection() as c:
        c.execute("UPDATE deliveries SET simulated=1,status='pending' WHERE status='not_connected'")


def step(store, channel, adapter):
    if channel not in {'telegram', 'sheets'} or not isinstance(adapter, TestAdapter):
        raise ValueError('Only named local test adapters are supported')
    now = store.settings.clock()
    with store.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        job = c.execute("SELECT * FROM deliveries WHERE channel=? AND simulated=1 AND status='pending' AND next_at<=? ORDER BY next_at LIMIT 1", (channel, now)).fetchone()
        if not job:
            return None
        c.execute("UPDATE deliveries SET status='processing',attempts=attempts+1 WHERE submission_id=? AND channel=?", (job['submission_id'], channel))
    try:
        outcome = adapter.send(job['submission_id'])
    except Exception:
        outcome = Outcome('unknown', 'interrupted')
    attempts = job['attempts'] + 1
    state = {'success': 'delivered', 'failed': 'failed', 'unknown': 'unknown',
             'retry': 'failed' if attempts >= 3 else 'pending'}[outcome.kind]
    with store.connection() as c:
        c.execute('UPDATE deliveries SET status=?,next_at=?,error=? WHERE submission_id=? AND channel=?',
                  (state, now + min(max(outcome.delay, 1), 300), outcome.reason, job['submission_id'], channel))
    return state


def channel_message(job):
    status = job['status']
    state = status.removeprefix('mock_').removeprefix('live_')
    if state == 'delivered':
        if job['channel'] == 'sheets':
            if status == 'delivered':
                return 'Успех записи сымитирован. Чтение и сверка строки не выполнялись.'
            return 'Запись подтверждена сверкой всех девяти полей.'
        return 'Создание сообщения подтверждено. Прочтение человеком не подтверждено.'
    if state == 'unknown' or status == 'live_processing':
        return 'Результат неизвестен. Автоматического повтора нет.'
    if state in {'queued', 'processing', 'pending'}:
        return 'Ожидает результата.'
    if state == 'failed':
        return 'Доставка не состоялась. Повтора не будет.'
    return 'Канал выключен.'


def delivery_message(jobs):
    """Без provider IDs/получателя/секретов; mock никогда не подтверждает живую доставку."""
    if not any(not j['simulated'] and j['status'].startswith('live_') for j in jobs):
        return 'Сохранено в демо-реестре, доставка не подключена.'
    messages = {
        'telegram': {
            'live_queued': 'Telegram ожидает отправки.',
            'live_delivered': 'Telegram подтвердил создание сообщения; прочтение человеком не подтверждено.',
            'live_unknown': 'Результат отправки в Telegram неизвестен; не повторяйте её без проверки.',
            'live_processing': 'Результат отправки в Telegram неизвестен; не повторяйте её без проверки.',
            'live_pending': 'Telegram пока не принял сообщение; разрешён только ограниченный ручной повтор.',
            'live_failed': 'Отправка в Telegram завершилась отказом.'},
        'sheets': {
            'live_queued': 'Google Sheets ожидает записи.',
            'live_delivered': 'Строка Google Sheets подтверждена чтением всех девяти значений.',
            'live_unknown': 'Результат записи Google Sheets неизвестен; повтор append запрещён, доступна только сверка чтением.',
            'live_processing': 'Результат записи Google Sheets неизвестен; повтор append запрещён, доступна только сверка чтением.',
            'live_pending': 'Запись Google Sheets пока не подтверждена; допустима только отложенная ручная попытка.',
            'live_failed': 'Операция Google Sheets завершилась отказом.'}}
    parts = ['Запрос сохранён.']
    for channel, label in (('telegram', 'Telegram'), ('sheets', 'Google Sheets')):
        job = next((j for j in jobs if j['channel'] == channel), None)
        if job and job['simulated']:
            parts.append(label + ': тестовый режим.')
        else:
            parts.append(messages[channel].get(job['status'] if job else '',
                                               label + (' не подключены.' if channel == 'sheets' else ' не подключён.')))
    return ' '.join(parts)
