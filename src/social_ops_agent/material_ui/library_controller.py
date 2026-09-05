"""Library presentation data and actions, independent of Qt widgets."""
from __future__ import annotations

import json
from pathlib import Path

from .selection_page import SelectionPage


class LibraryController:
    def __init__(self, service, runner=None):
        self.service = service
        self.runner = runner

    def list(self, *, query='', analysis_state=None, usage_state=None, limit=100, offset=0, **filters):
        return self.service.library().list(
            query=query, analysis_state=analysis_state, usage_state=usage_state,
            limit=limit, offset=offset, **filters,
        )

    def get(self, resource_id):
        return self.service.library().get(resource_id)

    @staticmethod
    def describe(row):
        metadata = json.loads(row['metadata_json'] or '{}')
        analysis = json.loads(row['analysis_json'] or '{}')
        features = json.loads(row['features_json'] or '{}')
        scores = json.loads(row['scores_json'] or '[]')
        text = (
            f'素材 ID：{row["id"]}\n主题：{metadata.get("theme") or "未分类"}'
            f'\n来源：{metadata.get("source_url") or row["source_path"]}\n文件：{row["path"]}'
            '\n基础评分：' + str(features.get('quality', '未分析'))
            + '\n' + str(analysis.get('summary') or analysis.get('error') or '')
        )
        for score in scores:
            text += (
                f'\n{score.get("strategy", "策略评分")}：'
                f'{score.get("score", score.get("status", "待配置"))}  '
                f'{score.get("recommendation", "")}'
            )
        return text

    def set_usage(self, resource_id, state):
        self.service.library().set_usage(resource_id, state)

    def review(self, resource_id, *, subject_group=None):
        self.service.confirm_review(resource_id, subject_group=subject_group)

    def rescore(self):
        return self.service.rescore()

    def issues(self, *, limit=100, offset=0):
        return self.service.library().attempts(limit=limit, offset=offset)

    def issue_page(self, *, cursor=None, limit=100, query=''):
        offset = cursor or 0
        rows = self.issues(limit=limit+1,offset=offset)
        entries = [(row['source_path'],Path(row['source_path']).name+'\n'+
                    '；'.join(json.loads(row['issues_json']))) for row in rows[:limit]]
        return SelectionPage(entries,offset+limit if len(rows)>limit else None)

    def retry_intake(self, paths):
        if self.runner is None:
            raise ValueError('请从工具箱打开素材库后重试')
        paths = list(dict.fromkeys(paths))
        if not paths:
            raise ValueError('请先选择需要重新检测的源文件')
        job_id = self.service.create('import', paths, trusted_local=True)
        self.runner.submit(job_id)
        return job_id
