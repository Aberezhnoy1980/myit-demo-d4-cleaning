"""Конфигурация только явной локальной команды. Импорт не читает файлы/окружение."""
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import os
import re
import stat


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class TelegramConfig:
    token: str = field(default='', repr=False)
    chat_id: str = field(default='', repr=False)
    enabled: bool = False

    def __post_init__(self):
        if self.token or self.chat_id or self.enabled:
            if not re.fullmatch(r'[0-9]{6,20}:[A-Za-z0-9_-]{30,80}', self.token):
                raise ConfigurationError('Неверный формат настройки Telegram. Значения скрыты.')
            if not re.fullmatch(r'-[1-9][0-9]{0,15}', self.chat_id) or abs(int(self.chat_id)) >= 2**52:
                raise ConfigurationError('Нужен числовой ID согласованной закрытой группы. Значение скрыто.')
        if type(self.enabled) is not bool:
            raise ConfigurationError('Неверный переключатель live.')

    def __repr__(self):
        return f'TelegramConfig(enabled={self.enabled}, configured={bool(self.token and self.chat_id)})'

    @property
    def binding(self):
        # Не храним token/chat_id в задании. Бот + получатель фиксируются первым claim.
        return hashlib.sha256((self.token.split(':', 1)[0] + ':' + self.chat_id).encode()).hexdigest()


def parse_config(text):
    if len(text.encode('utf-8')) > 4096:
        raise ConfigurationError('Файл настройки Telegram слишком большой.')
    allowed = {'D4_TELEGRAM_TOKEN', 'D4_TELEGRAM_CHAT_ID', 'D4_TELEGRAM_LIVE'}
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key not in allowed or key in values:
            raise ConfigurationError('В настройке Telegram неизвестный, повторный или некорректный ключ.')
        values[key] = value.strip()  # Данные без shell, source, eval, кавычек и подстановок.
    live = values.get('D4_TELEGRAM_LIVE', '0')
    if live not in {'0', '1'}:
        raise ConfigurationError('D4_TELEGRAM_LIVE должен быть 0 или 1.')
    return TelegramConfig(values.get('D4_TELEGRAM_TOKEN', ''), values.get('D4_TELEGRAM_CHAT_ID', ''), live == '1')


def load_config(path: Path):
    """Вызывать только после отдельного разрешения на чтение именно этого .env."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as file:
            meta = os.fstat(file.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) not in {0o400, 0o600}:
                raise ConfigurationError('Настройке Telegram нужны права 0600 или 0400 и текущий владелец.')
            raw = file.read(4097)
        if len(raw) > 4096:
            raise ConfigurationError('Файл настройки Telegram слишком большой.')
        return parse_config(raw.decode('utf-8'))
    except (OSError, UnicodeError):
        raise ConfigurationError('Настройка Telegram недоступна. Другие источники секретов не проверяются.') from None
