"""B2: только явная конфигурация; импорты не читают секреты/ADC/окружение."""
from dataclasses import dataclass, field
import hashlib
import importlib.metadata
import os
from pathlib import Path
import re
import stat

from .model import ROOT
from .telegram_config import ConfigurationError

TOKEN_URI = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/spreadsheets'
KEY_FILE = ROOT / '.secrets' / 'sheets-service-account.json'
MAX_ROW = 201  # шапка + максимум 200 записей; выделенный лист без иных данных


@dataclass(frozen=True, repr=False)
class SheetsConfig:
    spreadsheet_id: str = field(default='', repr=False)
    service_account: str = field(default='', repr=False)
    key_file: str = field(default='', repr=False)
    sheet: str = 'Leads'
    enabled: bool = False

    def __post_init__(self):
        if self.sheet != 'Leads' or type(self.enabled) is not bool:
            raise ConfigurationError('Неверный лист или переключатель Sheets.')
        if self.spreadsheet_id or self.service_account or self.key_file or self.enabled:
            if not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', self.spreadsheet_id):
                raise ConfigurationError('Неверный ID таблицы Sheets. Значение скрыто.')
            if not re.fullmatch(r'[a-z0-9-]{3,63}@[a-z0-9-]{6,63}\.iam\.gserviceaccount\.com', self.service_account):
                raise ConfigurationError('Неверная техническая учётная запись Sheets. Значение скрыто.')
            if self.key_file != str(KEY_FILE):
                raise ConfigurationError('Ключ Sheets должен иметь согласованный приватный путь.')

    def __repr__(self):
        return f'SheetsConfig(enabled={self.enabled}, configured={bool(self.spreadsheet_id and self.service_account)})'

    @property
    def binding(self):
        value = f'{self.spreadsheet_id}:{self.sheet}:{MAX_ROW}:{self.service_account}'
        return hashlib.sha256(value.encode()).hexdigest()


def parse_config(text):
    if len(text.encode('utf-8')) > 4096:
        raise ConfigurationError('Настройка Sheets слишком большая.')
    allowed = {'D4_SHEETS_SPREADSHEET_ID', 'D4_SHEETS_SERVICE_ACCOUNT', 'D4_SHEETS_KEY_FILE',
               'D4_SHEETS_SHEET', 'D4_SHEETS_LIVE'}
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in allowed or key in values:
            raise ConfigurationError('Неизвестный, повторный или неверный ключ Sheets.')
        values[key] = value.strip()
    live = values.get('D4_SHEETS_LIVE', '0')
    if live not in {'0', '1'}:
        raise ConfigurationError('D4_SHEETS_LIVE должен быть 0 или 1.')
    return SheetsConfig(values.get('D4_SHEETS_SPREADSHEET_ID', ''), values.get('D4_SHEETS_SERVICE_ACCOUNT', ''),
                        values.get('D4_SHEETS_KEY_FILE', ''), values.get('D4_SHEETS_SHEET', 'Leads'), live == '1')


def read_private(path, limit=4096, *, dir_fd=None):
    """Читает только заданный приватный обычный файл."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        with os.fdopen(fd, 'rb') as file:
            meta = os.fstat(file.fileno())
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                    or stat.S_IMODE(meta.st_mode) not in {0o400, 0o600}):
                raise ConfigurationError('Приватному файлу Sheets нужны текущий владелец и права 0600/0400.')
            raw = file.read(limit + 1)
        if len(raw) > limit:
            raise ConfigurationError('Приватный файл Sheets слишком большой.')
        return raw.decode('utf-8')
    except (OSError, UnicodeError):
        raise ConfigurationError('Приватный файл Sheets недоступен. Другие источники не проверяются.') from None


def load_config(path):
    return parse_config(read_private(path))


def validate_service_account(info, config):
    """Проверка до будущей передачи SDK. Не подписывает JWT и не выполняет OAuth."""
    allowed = {'type', 'project_id', 'private_key_id', 'private_key', 'client_email', 'client_id',
               'auth_uri', 'token_uri', 'auth_provider_x509_cert_url', 'client_x509_cert_url', 'universe_domain'}
    if (not isinstance(info, dict) or set(info) - allowed or info.get('type') != 'service_account'
            or info.get('client_email') != config.service_account or info.get('token_uri') != TOKEN_URI
            or info.get('universe_domain', 'googleapis.com') != 'googleapis.com'
            or not isinstance(info.get('private_key'), str) or not info['private_key']):
        raise ConfigurationError('JSON service account не соответствует согласованной конфигурации.')
    # Эти URL SDK не нужны для refresh; не передавать произвольные endpoint даже неиспользуемыми.
    expected = {'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                'auth_provider_x509_cert_url': 'https://www.googleapis.com/oauth2/v1/certs',
                'client_x509_cert_url': 'https://www.googleapis.com/robot/v1/metadata/x509/' + config.service_account}
    from urllib.parse import unquote
    if any(key in info and (not isinstance(info[key], str) or unquote(info[key]) != value)
           for key, value in expected.items()):
        raise ConfigurationError('Не разрешён endpoint из JSON service account.')
    return None


def require_live_sdk():
    try:
        importlib.metadata.version('google-auth')
        importlib.metadata.version('requests')
    except importlib.metadata.PackageNotFoundError:
        raise ConfigurationError('Google auth SDK/транспорт не установлены. Live Sheets выключен; секреты не читались.') from None
    # Наличие пакета не равно принятию его авторизации/повторов. До следующего согласованного этапа закрыто.
    raise ConfigurationError('Live Sheets ещё не реализован и не проверен с SDK. Секреты не читались.')
