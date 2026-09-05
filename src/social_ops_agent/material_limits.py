"""Cross-process local-model slots, including separate Harness MCP processes."""
from contextlib import contextmanager
from pathlib import Path
import time

from .browser_queue import open_lock
from .process_locks import _try_lock, _unlock


@contextmanager
def model_slot(root, count=1, timeout=1800, *, check_control=None):
    """Acquire a process-wide model slot, honouring task control while queued.

    ``check_control`` may raise a task interruption. The context manager always
    releases its handles, including when interruption arrives just after acquire.
    """
    if count < 1:
        raise ValueError('本地模型并发数量必须大于零')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    streams = []
    acquired = None
    started = time.monotonic()
    try:
        for index in range(count):
            streams.append(open_lock(root / f'model-{index}.lock'))
        while acquired is None:
            if check_control is not None:
                check_control()
            for stream in streams:
                if _try_lock(stream):
                    acquired = stream
                    break
            if acquired is None:
                if time.monotonic()-started > timeout:
                    raise TimeoutError('等待本地模型资源超时，可稍后重试')
                time.sleep(.1)
        if check_control is not None:
            check_control()
        yield
    finally:
        if acquired is not None:
            _unlock(acquired)
        for stream in streams:
            stream.close()
