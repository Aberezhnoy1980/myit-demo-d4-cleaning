"""Подготовка двух каналов для явно включённого закрытого прогона."""
import json
import os
import platform
import socket
import stat

from . import sheets_config, telegram_config
from .model import ROOT
from .mac_auth import MacAuthorization, OPENSSL
from .sheets import SheetsAdapter
from .sheets_auth import SDKAuthorization
from .telegram import TelegramAdapter
from .telegram_config import ConfigurationError


def build_closed_delivery():
    try:
        release = platform.freedesktop_os_release() if platform.system() == 'Linux' else {}
    except OSError:
        release = {}
    if (release.get('ID') != 'debian' or release.get('VERSION_ID') != '12'
            or socket.gethostname().lower() != 'd4-debian12'):
        raise ConfigurationError('Закрытый прогон разрешён только в D4-Debian12.')

    return _build_delivery(SDKAuthorization)


def build_mac_demo_delivery():
    if platform.system() != 'Darwin' or not os.access(OPENSSL, os.X_OK):
        raise ConfigurationError('Разовая демонстрация требует Mac и системный OpenSSL.')
    return _build_delivery(MacAuthorization)


def _build_delivery(authorization_type):
    telegram = telegram_config.load_config(ROOT / '.env')
    sheets = sheets_config.load_config(ROOT / '.env.sheets')
    if not telegram.enabled or not sheets.enabled:
        raise ConfigurationError('Для закрытого прогона оба переключателя live должны быть включены.')

    key = sheets_config.KEY_FILE
    try:
        directory = os.open(key.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            meta = os.fstat(directory)
            if meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) not in {0o700, 0o500}:
                raise ConfigurationError('Каталогу ключа Sheets нужны текущий владелец и права 0700/0500.')
            info = json.loads(sheets_config.read_private(key.name, limit=16384, dir_fd=directory))
        finally:
            os.close(directory)
    except (OSError, ValueError):
        raise ConfigurationError('Не удалось прочитать согласованный приватный ключ Sheets.') from None

    authorization = authorization_type(sheets, info, allow_live=True)
    return TelegramAdapter(telegram, allow_live=True), SheetsAdapter(sheets, authorization, allow_live=True)
