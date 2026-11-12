"""Preview static files only. No form storage or delivery. Loopback only."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse
import functools

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=4344)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1] / 'public'
class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
        super().end_headers()
    def list_directory(self, path):
        self.send_error(404)
        return None

print(f'D4: http://127.0.0.1:{args.port} — только визуальный прототип', flush=True)
ThreadingHTTPServer(('127.0.0.1', args.port), functools.partial(Handler, directory=str(root))).serve_forever()
