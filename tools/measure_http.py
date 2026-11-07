"""Измерение фактически выданных тел HTTP, не браузерный cold-load/Web Vitals."""
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
import argparse
import gzip
import json

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base', default='http://127.0.0.1:4344/')
args = parser.parse_args()
parts = urlsplit(args.base)
if parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', 'localhost'} or not args.base.endswith('/'):
    parser.error('Нужен loopback base URL с завершающим /')
resources = ['', 'style.css', 'app.js', 'assets/mark.svg', 'assets/sofa-700.jpg', 'assets/sofa-1400.jpg']
rows = []
for name in resources:
    with urlopen(args.base + name, timeout=10) as response:
        content = response.read()
        rows.append({'resource': name or 'index.html', 'status': response.status, 'body_bytes': len(content)})
        if name == 'app.js':
            js_gzip = len(gzip.compress(content, mtime=0))
report = {'method': __doc__, 'resources': rows,
          'mobile_700_body_bytes': sum(r['body_bytes'] for r in rows if r['resource'] != 'assets/sofa-1400.jpg'),
          'desktop_1400_body_bytes': sum(r['body_bytes'] for r in rows if r['resource'] != 'assets/sofa-700.jpg'),
          'all_bodies_bytes': sum(r['body_bytes'] for r in rows), 'js_gzip_estimate_bytes': js_gzip}
target = Path(__file__).resolve().parents[1] / 'docs/server-03/http-resources.json'
target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(report, ensure_ascii=False, indent=2))
