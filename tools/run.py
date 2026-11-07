"""Запуск D4 только на loopback; один процесс, без reload и access log."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.app import create_app
from server.model import Settings
import uvicorn

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4344)
    parser.add_argument('--prefix', default='')
    args = parser.parse_args()
    uvicorn.run(create_app(Settings(prefix=args.prefix)), host='127.0.0.1', port=args.port,
                access_log=False, log_level='warning', server_header=False)
