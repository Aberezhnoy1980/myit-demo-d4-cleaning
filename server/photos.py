from pathlib import Path
import os
import subprocess
import sys
import threading

from .model import Failure, MIB

WORKER = Path(__file__).with_name('image_worker.py')
DECODERS = threading.BoundedSemaphore(2)


def monitor_memory(proc, stop, failed):
    # Только PID своего дочернего процесса. Порог RSS с выборкой 100 мс, не kernel hard limit.
    while not stop.wait(0.1):
        if proc.poll() is not None:
            return
        try:
            rss = int(subprocess.check_output(['/bin/ps', '-o', 'rss=', '-p', str(proc.pid)],
                                              stderr=subprocess.DEVNULL, timeout=0.5).strip())
            if rss <= 384 * 1024:
                continue
        except (ValueError, OSError, subprocess.SubprocessError):
            if proc.poll() is not None:
                return
        failed.set()
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return


def decode_photo(source, mime):
    if mime not in {'image/jpeg', 'image/png', 'image/webp'} or not source or len(source) > 5 * MIB:
        raise Failure(422, 'Проверьте фотографии.', {'photos': 'Нужны JPEG, PNG или WebP до 5 МБ каждое.'})
    if not DECODERS.acquire(timeout=0.1):
        raise Failure(429, 'Обработка фото занята. Повторите запрос чуть позже.', retry_after=5)
    try:
        stop, failed = threading.Event(), threading.Event()
        with subprocess.Popen(
                [sys.executable, '-I', str(WORKER), mime], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'PATH': os.defpath, 'PYTHONNOUSERSITE': '1'},
        ) as proc:
            watcher = threading.Thread(target=monitor_memory, args=(proc, stop, failed), daemon=True)
            watcher.start()
            try:
                output, _ = proc.communicate(source, timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
                raise Failure(422, 'Проверьте фотографии.', {'photos': 'Фото не удалось обработать за отведённое время.'}) from None
            finally:
                stop.set()
                watcher.join(timeout=1)
            returncode = proc.returncode
        if failed.is_set() or returncode or not output or len(output) > 5 * MIB:
            raise Failure(422, 'Проверьте фотографии.', {'photos': 'Фото повреждено, тип не совпадает или превышен лимит 20 Мп. Выберите другое.'})
        return output
    finally:
        DECODERS.release()
