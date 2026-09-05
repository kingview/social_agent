"""Shared task-state semantics, independent of Qt, SQLite and plugin execution."""
from enum import StrEnum


class TaskState(StrEnum):
    QUEUED = '待执行'
    RUNNING = '执行中'
    PAUSED = '已暂停'
    REVIEW = '待人工处理'
    COMPLETED = '已完成'
    PARTIAL = '部分完成'
    FAILED = '执行失败'
    STOPPED = '已停止'


STATES = tuple(state.value for state in TaskState)
TASK_GROUPS = {
    'active': frozenset((TaskState.QUEUED, TaskState.RUNNING)),
    'attention': frozenset((TaskState.PAUSED, TaskState.REVIEW, TaskState.PARTIAL, TaskState.FAILED)),
    'ended': frozenset((TaskState.COMPLETED, TaskState.STOPPED)),
}
RESUMABLE = frozenset((TaskState.PAUSED, TaskState.REVIEW, TaskState.PARTIAL, TaskState.FAILED, TaskState.STOPPED))


class MaterialJobInterrupted(Exception):
    """A cooperative checkpoint, not a failed item or plugin exception."""
    def __init__(self, command):
        if command not in {'pause', 'stop'}:
            raise ValueError('无效中断指令')
        self.command = command
        super().__init__('任务已暂停' if command == 'pause' else '任务已停止')


def material_actions(row):
    # Old task records may retain the already-acknowledged pause/stop command.
    # Their settled state is authoritative and must remain resumable.
    if row['state'] in RESUMABLE:
        return ['resume', 'retry']
    if row['state'] in TASK_GROUPS['active']:
        return [] if row.get('command') else ['pause', 'stop']
    return []


def item_status(result):
    if not isinstance(result, dict):
        raise ValueError('工具返回的项目结果必须为对象')
    if result.get('needs_human_review') or result.get('analysis_state') == '需复核':
        return 'review'
    if result.get('intake_state') == '未通过' or result.get('completed') is False:
        return 'failed'
    return 'completed'


def outcome(results, total):
    if any(item.get('status') == 'review' for item in results.values()):
        return TaskState.REVIEW.value
    completed = sum(item.get('status') == 'completed' for item in results.values())
    return (TaskState.COMPLETED if completed == total else
            TaskState.PARTIAL if completed else TaskState.FAILED).value
