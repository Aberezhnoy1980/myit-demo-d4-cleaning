"""Локальные команды администратора. Не публиковать как HTTP endpoint."""
from datetime import datetime, timezone
import argparse
import fcntl
import secrets
import sqlite3

from .delivery import TestAdapter, arm_test_jobs, step
from .model import Failure, Settings, ROOT
from .store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    clean = sub.add_parser('cleanup')
    clean.add_argument('--apply', action='store_true')
    sub.add_parser('backup')
    restore = sub.add_parser('restore')
    restore.add_argument('filename')
    sim = sub.add_parser('simulate')
    sim.add_argument('--channel', choices=['telegram', 'sheets'], required=True)
    sim.add_argument('--scenario', choices=['success', 'unavailable', 'rate_limit', 'timeout', 'forbidden'], required=True)
    telegram = sub.add_parser('telegram-send', help='Будущий один live-тест, по умолчанию выключен')
    telegram.add_argument('--submission-id', required=True)
    telegram.add_argument('--execute-live', action='store_true', help='Только после отдельного согласования чтения .env и отправки')
    for name in ('sheets-send', 'sheets-reconcile'):
        sheets = sub.add_parser(name, help='Будущий B2: пока live закрыт до установки/приёмки SDK')
        sheets.add_argument('--submission-id', required=True)
        sheets.add_argument('--execute-live', action='store_true')
    args = parser.parse_args()
    if args.command in {'telegram-send', 'sheets-send', 'sheets-reconcile'} and not args.execute_live:
        parser.exit(2, 'Живая отправка выключена. Настройка и секреты не читались.\n')
    if args.command.startswith('sheets-'):
        from .sheets_config import ConfigurationError, require_live_sdk
        try:
            require_live_sdk()  # до config/data: SDK пока отсутствует, секреты не читать
        except ConfigurationError as error:
            parser.exit(2, str(error) + '\n')
    store = Store(Settings())
    # Администраторские изменения только при остановленном сервере.
    store.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (store.settings.data_dir / '.server.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, 'Сначала остановите сервер D4 или дождитесь другой команды администратора.\n')
        # Restore не должен менять даже processing-статусы текущей базы до проверки backup.
        if args.command not in {'restore', 'telegram-send', 'sheets-send', 'sheets-reconcile'}:
            store.initialize()
        if args.command == 'cleanup':
            count = store.cleanup(args.apply)
            expired_backups = []
            for p in (ROOT / 'backups').glob('*.sqlite3'):
                if not p.is_symlink() and p.stat().st_mtime <= store.settings.clock() - store.settings.retention:
                    expired_backups.append(p)
                    if args.apply:
                        p.unlink()
            print(f'Истекшие заявки: {count}; backup: {len(expired_backups)}. Применено: {args.apply}')
        elif args.command == 'backup':
            name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4) + '.sqlite3'
            store.backup(ROOT / 'backups' / name)
            print('Backup создан: backups/' + name)
        elif args.command == 'restore':
            name = args.filename
            if '/' in name or '\\' in name or not name.endswith('.sqlite3'):
                parser.error('Укажите имя файла внутри backups, без пути.')
            source = ROOT / 'backups' / name
            if source.is_symlink() or not source.is_file():
                parser.error('Backup не найден.')
            safety = ROOT / 'backups' / ('before-restore-' + secrets.token_hex(8) + '.sqlite3')
            try:
                store.backup(safety)
                store.restore(source)
            except (ValueError, OSError, sqlite3.Error, TimeoutError):
                parser.exit(2, 'Восстановление не выполнено. Проверьте совместимость backup и доступ к файлам.\n')
            print('Восстановлено. Копия прежнего реестра сохранена в backups. Внешних отправок нет.')
        elif args.command == 'simulate':
            arm_test_jobs(store)
            outcome = step(store, args.channel, TestAdapter(args.scenario))
            print('Локальная имитация:', outcome or 'нет готовых заданий', '— внешних отправок нет.')
        elif args.command.startswith('sheets-'):
            from .sheets import build_live_adapter, send_one as sheets_one
            from .sheets_config import load_config as load_sheets
            try:
                if not store.path.is_file() or store.path.is_symlink():
                    parser.exit(2, 'Сначала нужен существующий локальный реестр.\n')
                config = load_sheets(ROOT / '.env.sheets')
                if not config.enabled:
                    parser.exit(2, 'Live Sheets выключен.\n')
                outcome = sheets_one(store, args.submission_id, build_live_adapter(config),
                                     reconcile=args.command == 'sheets-reconcile')
            except (ConfigurationError, Failure) as error:
                parser.exit(2, (error.message if isinstance(error, Failure) else str(error)) + '\n')
            except Exception:
                parser.exit(2, 'Команда Sheets не выполнена. Содержимое ошибки скрыто.\n')
            messages = {'delivered': 'Строка Sheets подтверждена чтением; Telegram не отправлялся.',
                        'pending': 'Запись Sheets не подтверждена; допустима только отложенная ручная попытка.',
                        'unknown': 'Результат Sheets неизвестен. Повтор append запрещён; доступна только адресная сверка.',
                        'failed': 'Операция Sheets завершилась отказом. Заявка и Telegram сохранены.'}
            print(messages[outcome.kind])
        else:
            from .telegram import TelegramAdapter, send_one
            from .telegram_config import ConfigurationError, load_config
            try:
                if not store.path.is_file() or store.path.is_symlink():
                    parser.exit(2, 'Сначала нужен существующий локальный реестр.\n')
                config = load_config(ROOT / '.env')
                adapter = TelegramAdapter(config, allow_live=args.execute_live)
                outcome = send_one(store, args.submission_id, adapter)
            except (ConfigurationError, Failure) as error:
                message = error.message if isinstance(error, Failure) else str(error)
                parser.exit(2, message + '\n')
            except Exception:
                parser.exit(2, 'Команда Telegram не выполнена. Проверьте локальную настройку, состояние заявки и разрешение повтора.\n')
            if outcome.kind == 'delivered':
                print('Telegram подтвердил создание сообщения; прочтение человеком не подтверждено.')
            elif outcome.kind == 'pending':
                print('Сообщение не подтверждено. Следующая попытка разрешена позже, только отдельной командой.')
            elif outcome.kind == 'unknown':
                print('Результат отправки неизвестен. Повтор без отдельной проверки запрещён.')
            else:
                print('Отправка завершилась отказом. Локальная заявка сохранена.')


if __name__ == '__main__':
    main()
