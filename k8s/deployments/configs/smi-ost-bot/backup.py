import datetime
import os
from pathlib import Path
import sqlite3
import time


def backup():
    source = Path('/data/smi_ost.db')
    if not source.is_file():
        raise RuntimeError('Source database does not exist')
    folder = Path('/backups')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = folder / f'smi-ost-{stamp}.db'
    temporary = target.with_suffix('.tmp')
    try:
        with sqlite3.connect(f'file:{source}?mode=ro', uri=True, timeout=30) as src:
            with sqlite3.connect(temporary) as dst:
                src.backup(dst, pages=256, sleep=0.1)
                result = dst.execute('PRAGMA integrity_check').fetchall()
                if result != [('ok',)]:
                    raise RuntimeError(f'Backup integrity check failed: {result}')
                dst.execute('PRAGMA journal_mode=DELETE')
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        cutoff = time.time() - 30 * 86400
        for old in folder.glob('smi-ost-*.db'):
            if old.stat().st_mtime < cutoff:
                old.unlink()
        print(f'Backup verified: {target.name}', flush=True)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    os.umask(0o077)
    while True:
        backup()
        time.sleep(3600)
