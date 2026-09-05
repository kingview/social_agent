"""One task-control boundary over the existing Agent and material journals.

The facade does not execute natural-language commands or merge the databases.
Agent actions are delegated to the owning conversation and Harness; material
actions retain their item checkpoints and the original configuration snapshot.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .material_task_state import TASK_GROUPS, material_actions
from .task_store import TaskStore
from .transfer_progress import format_transfer


class TaskCenter:
    def __init__(self, service, runner, agent_command=None, *, agent_actions=None):
        self.service, self.runner = service, runner
        self.agent_command, self.agent_actions = agent_command, agent_actions
        # Migration/initialization is not repeated by the one-second UI timer.
        self.agent_store = TaskStore(service.state_root)

    def _normalize(self, row):
        row = dict(row)
        if row.get('kind') == 'agent':
            row['actions'] = list(self.agent_actions(row)) if self.agent_actions and self.agent_command else []
        else:
            row['kind'] = 'material'
            row['actions'] = material_actions(row)
        return row

    def list(self, *, tool=None, group=None, query='', limit=100, offset=0):
        if group is not None and group not in TASK_GROUPS:
            raise ValueError('无效任务分组')
        limit, offset = max(0, int(limit)), max(0, int(offset))
        if not limit:
            return []
        states = TASK_GROUPS.get(group)
        # Each store contributes only the prefix needed for this merged page.
        # No per-task full-result queries occur until a row is selected.
        count = limit + offset
        rows = [] if tool == 'agent' else self.service.jobs.list(
            tool=tool, states=states, query=query, limit=count, summary=True,
        )
        if tool in (None, 'agent'):
            rows += self.agent_store.dashboard(query=query, states=states, limit=count, summary=True)
        rows.sort(key=_sort_key, reverse=True)
        return [self._normalize(row) for row in rows[offset:offset + limit]]

    def get(self, task_id):
        if task_id.startswith('agent:'):
            row = self.agent_store.dashboard_task(task_id.removeprefix('agent:'))
        else:
            row = self.service.jobs.get(task_id)
        return self._normalize(row)

    def command(self, task_id, command):
        row = self.get(task_id)  # Revalidate; controls may show a stale snapshot.
        if command not in row['actions']:
            raise ValueError('任务状态已变化，当前不支持此操作，请刷新后重试')
        if row['kind'] == 'agent':
            self.agent_command(row, command)
        else:
            self.service.jobs.command(task_id, command)
            if command in {'resume', 'retry'}:
                self.runner.submit(task_id)

    def describe(self, row, *, technical=False):
        if technical:
            return json.dumps(row, ensure_ascii=False, indent=2)
        completed, total = row['completed'], row['total']
        lines = [f'{row["name"]} · {row["state"]}', f'已完成 {completed}/{total} 项（{int(100 * completed / max(1, total))}%）']
        if row.get('command'):
            lines.append('正在请求暂停，等待当前安全检查点。' if row['command'] == 'pause' else '正在请求停止，已完成结果会保留。')
        if row.get('error'):
            lines.append(str(row['error']))
        if row['kind'] == 'agent':
            result = row.get('results') or {}
            if result.get('summary'):
                lines.append(str(result['summary']))
            for index, step in enumerate(row.get('items', []), 1):
                lines.append(f'{index}. {step}')
            lines.append('继续任务将回到所属对话，由 Harness 基于原任务与已完成结果规划；不会直接重放发布操作。')
        else:
            results = row.get('results') or {}
            for index, item in enumerate(row.get('items', [])):
                checkpoint = results.get(str(index), {})
                label = {'completed': '已完成', 'failed': '失败', 'review': '待复核', 'partial':'部分完成'}.get(checkpoint.get('status'), '待处理')
                lines.append(f'{index + 1}. {item} · {label}')
                payload = checkpoint.get('result') or {}
                detail = checkpoint.get('error') or payload.get('error') or payload.get('summary')
                if detail:
                    lines.append(str(detail))
                if payload.get('issues'):
                    lines.append('；'.join(map(str, payload['issues'])))
                if 'requested' in payload and 'found' in payload:
                    lines.append(f'有效链接 {payload.get("count",0)}/{payload["requested"]}；发现 {payload["found"]}；'
                                 f'筛选排除 {payload.get("filtered_out",0)}；重复 {payload.get("skipped_duplicates",0)}')
                if payload.get('warnings'):
                    lines.extend(map(str,payload['warnings']))
                target = self._payload_directory(payload)
                if target:
                    lines.append('输出：' + str(target))
            telemetry = self._transfer(row)
            if telemetry:
                lines.append(telemetry)
        return '\n'.join(lines)

    def _transfer(self, row):
        if row['state'] != '执行中':
            return None
        execution_id = (row.get('parameters') or {}).get('execution_id') or row['id']
        if not isinstance(execution_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', execution_id):
            return None
        path = self.service.state_root / 'transfer-progress' / f'{execution_id}.json'
        try:
            with path.open('rb') as stream:
                content = stream.read(65537)
            if len(content) > 65536:
                return None
            event = json.loads(content)
            if event.get('execution_id') != execution_id:
                return None
            return format_transfer(event)
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _payload_directory(payload):
        value = payload.get('output_directory')
        if value:
            return Path(value)
        value = payload.get('report_path') or payload.get('path')
        if value:
            return Path(value).parent
        for artifact in payload.get('artifacts') or []:
            if isinstance(artifact, dict) and artifact.get('path'):
                return Path(artifact['path']).parent
        return None

    def output_directory(self, row):
        if not row:
            return None
        results = row.get('results') or {}
        if row.get('kind') == 'agent':
            candidates = [results]
        else:
            candidates = [value.get('result') or {} for value in results.values()]
        for payload in candidates:
            path = self._payload_directory(payload)
            if path and path.is_dir():
                return path
        return None


def _sort_key(row):
    try:
        timestamp = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, KeyError):
        timestamp = 0
    return timestamp, row['id']
