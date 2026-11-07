"""Временный loopback QA: настоящие 404 для JS/фото, без приёма заявок."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import argparse
import functools

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=4345)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1] / 'public'


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/app.js' or path.startswith('/assets/sofa-'):
            return self.send_error(404)
        super().do_GET()

    def log_message(self, *args):
        pass


ThreadingHTTPServer(('127.0.0.1', args.port), functools.partial(Handler, directory=str(root))).serve_forever()
