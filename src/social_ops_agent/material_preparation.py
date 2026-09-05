"""Cancellable input preparation. No Qt objects or task writes while scanning."""
import os
from pathlib import Path
from threading import Event, Lock

MEDIA_SUFFIXES = frozenset({
    '.jpg','.jpeg','.png','.webp','.bmp','.tif','.tiff','.gif',
    '.mp4','.mov','.mkv','.webm','.avi','.m4v',
})
MAX_ITEMS = 500


class PreparationCancelled(Exception):
    pass


class PreparationControl:
    """Cancel is non-blocking with respect to filesystem/DB work.

    seal() atomically ends the cancellable phase immediately before task creation.
    After that point the durable task must be stopped through the task center.
    """
    def __init__(self):
        self._cancelled = Event()
        self._guard = Lock()
        self._sealed = False
        self._count = 0

    def check(self):
        if self._cancelled.is_set():
            raise PreparationCancelled('已取消任务准备，未创建任务')

    def cancel(self):
        with self._guard:
            if self._sealed:
                return False
            self._cancelled.set()
            return True

    def seal(self):
        with self._guard:
            self.check()
            self._sealed = True

    def progress(self, count):
        with self._guard:
            self._count = count

    @property
    def count(self):
        with self._guard:
            return self._count


def directory_files(root, check):
    # scandir streams directory entries, unlike sorted(rglob(...)) which must
    # traverse the entire tree before enforcing limits or responding to cancel.
    pending = [root]
    while pending:
        check()
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                check()
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif not entry.is_dir():
                    yield Path(entry.path)


def expand_items(tool, items, output_root, *, trusted_local=False,
                 check_cancel=None, on_progress=None):
    check = check_cancel or (lambda: None)
    expanded, seen = [], set()

    def add(value):
        if value not in seen:
            seen.add(value)
            expanded.append(value)
            if len(expanded)>MAX_ITEMS:
                raise ValueError('文件超过 500 个，请分批处理')
            if on_progress:
                on_progress(len(expanded))

    for item in items:
        check()
        if tool not in {'import','analyze'} or str(item).startswith('resource:'):
            add(item)
            continue
        path = Path(item).expanduser().resolve(strict=True)
        if not trusted_local and not path.is_relative_to(output_root):
            raise ValueError('Agent 只能使用输出目录中的媒体，其他本地文件请通过工具箱明确选择')
        directory = path.is_dir()
        candidates = directory_files(path,check) if directory and trusted_local else iter((path,))
        start = len(expanded)
        try:
            for candidate in candidates:
                check()
                resolved = candidate.resolve()
                if (resolved.suffix.lower() in MEDIA_SUFFIXES and resolved.is_file()
                        and (not directory or resolved.is_relative_to(path))):
                    add(str(resolved))
        finally:
            close = getattr(candidates,'close',None)
            if close:
                close()
        # Stable order without enumerating more than the allowed media count.
        if directory:
            expanded[start:] = sorted(expanded[start:])
    check()
    return expanded
