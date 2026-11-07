"""Write the prototype manifest, excluding itself, private data and caches."""
from pathlib import Path
import hashlib

root = Path(__file__).resolve().parents[1]
files = [root / name for name in ('README.md', '.gitignore', '.env.example', '.env.sheets.example', 'requirements-server.txt', 'requirements-test.txt',
                                 'constraints-macos-py311.txt', 'requirements-lock-macos-py311.txt')]
for area in ('public', 'docs', 'tools', 'server', 'tests'):
    files.extend(p for p in (root / area).rglob('*')
                 if p.is_file() and not p.is_symlink()
                 and 'review-coordinator' not in p.relative_to(root).parts
                 and '__pycache__' not in p.parts
                 and not any(part in {'.venv', '.pytest_cache', 'data', 'uploads', 'backups'} for part in p.relative_to(root).parts)
                 and p.suffix not in {'.pyc', '.db', '.sqlite', '.sqlite3', '.pem', '.key', '.p12', '.pfx'}
                 and not p.name.startswith('.env')
                 and not p.name.endswith('.credentials.json')
                 and p.name != 'manifest.sha256')
lines = [f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root).as_posix()}\n'
         for p in sorted(files)]
(root / 'docs/manifest.sha256').write_text(''.join(lines), encoding='utf-8')
print(f'{len(files)} files; manifest SHA-256: '
      + hashlib.sha256((root / 'docs/manifest.sha256').read_bytes()).hexdigest())
