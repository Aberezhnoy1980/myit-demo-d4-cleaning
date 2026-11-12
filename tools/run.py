"""Запуск D4 только на loopback; один процесс, без reload и access log."""
from pathlib import Path
import argparse
import os
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.app import create_app
from server.closed import build_closed_delivery, build_mac_demo_delivery
from server.model import ROOT, Settings
from server.telegram_config import ConfigurationError
import uvicorn


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4344)
    parser.add_argument('--prefix', default='')
    parser.add_argument('--registry-dir', type=Path, help='Абсолютный путь к каталогу реестра')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--closed-live', action='store_true', help='Явно включить закрытый прогон VM')
    modes.add_argument('--mac-demo-live', action='store_true', help='Разовая демонстрация на Mac, одна заявка')
    parser.add_argument('--submission-limit', type=int, help='Максимум новых заявок закрытого прогона, от 1 до 200')
    args = parser.parse_args(argv)
    if args.registry_dir is not None and not args.registry_dir.is_absolute():
        parser.error('Путь к реестру должен быть абсолютным.')
    settings = Settings(prefix=args.prefix, **({'data_dir': args.registry_dir} if args.registry_dir else {}))
    if args.closed_live or args.mac_demo_live:
        if args.mac_demo_live and args.submission_limit is not None:
            parser.error('Разовая демонстрация Mac имеет фиксированный лимит 1, без --submission-limit.')
        if not args.registry_dir or (args.closed_live and (args.submission_limit is None or not 1 <= args.submission_limit <= 200)):
            parser.error('Нужны --registry-dir и --submission-limit от 1 до 200.')
        if (settings.data_dir.is_relative_to(ROOT) or args.registry_dir.is_symlink() or settings.data_dir.exists()):
            parser.error('Закрытому прогону нужен новый каталог реестра вне исходников D4.')
        try:
            parent = settings.data_dir.parent.stat()
            if (not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) != 0o700
                    or parent.st_uid != os.getuid()):
                raise OSError()
        except OSError:
            parser.error('Родительскому каталогу реестра нужны текущий владелец и права 0700.')
        try:
            if args.mac_demo_live:
                app = create_app(settings, mac_demo_delivery=build_mac_demo_delivery())
            else:
                app = create_app(settings, closed_delivery=build_closed_delivery(), live_limit=args.submission_limit)
        except ConfigurationError as error:
            parser.error(str(error))
    else:
        if args.submission_limit is not None:
            parser.error('--submission-limit используется только с --closed-live.')
        app = create_app(settings)
    uvicorn.run(app, host='127.0.0.1', port=args.port, workers=1, proxy_headers=False,
                access_log=False, log_level='warning', server_header=False)


if __name__ == '__main__':
    main()
