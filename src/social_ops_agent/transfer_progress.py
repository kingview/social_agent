"""Read trusted Tool transfer telemetry without changing step percentages."""
import json
import time
from pathlib import Path


class TransferProgressReader:
    def __init__(self, root: Path, execution_id: str, notify, *, stall_seconds=120):
        self.path = root / 'transfer-progress' / f'{execution_id}.json'
        self.execution_id = execution_id
        self.notify = notify
        self.sequence = None
        self.signature = None
        self.last_advance = time.monotonic()
        self.last_display = 0.0
        self.last = None
        self.stall_seconds = stall_seconds

    def poll(self):
        try:
            item = json.loads(self.path.read_text())
            if item.get('execution_id') != self.execution_id:
                return False
        except (OSError, ValueError):
            return False
        now = time.monotonic()
        active = item.get('status') in {'downloading', 'preparing', 'locating'}
        signature = (item.get('filename'), item.get('downloaded_bytes'), item.get('files_completed'), item.get('post_index'))
        advanced = signature != self.signature
        if advanced:
            self.last_advance = now
            self.signature = signature
        if active and now-self.last_advance >= self.stall_seconds:
            raise TimeoutError(f'下载连续 {self.stall_seconds} 秒没有新增数据或文件进展，已停止任务；已保存文件保留。')
        if item.get('sequence') != self.sequence:
            self.sequence = item.get('sequence')
            if now-self.last_display >= 2 or not self.last or item.get('status') != self.last.get('status') or item.get('filename') != self.last.get('filename'):
                self.notify(format_transfer(item))
                self.last_display = now
            self.last = item
        return advanced


def format_transfer(item):
    status = item.get('status')
    if status == 'failed':
        return f"下载异常：{item.get('message') or '请查看日志'}"
    if status == 'completed':
        return f"本批下载完成：{item.get('files_completed', 0)} 个媒体文件。"
    index, count = item.get('post_index', 0), item.get('post_total', 0)
    name = item.get('filename') or '正在定位/加载媒体'
    size, total = item.get('downloaded_bytes', 0), item.get('total_bytes', 0)
    speed = item.get('speed_bps', 0)
    size_text = f'{size/1048576:.1f} / {total/1048576:.1f} MiB' if total else f'{size/1048576:.1f} MiB / 大小待确认'
    eta = f'，预计剩余 {(total-size)/speed/60:.1f} 分钟' if speed > 0 and total > size else ''
    return (f'下载帖子 {index}/{count} · {name}\n'
            f'{size_text} · {speed/1024:.0f} KiB/s{eta} · 已完成 {item.get("files_completed", 0)} 个媒体文件')
