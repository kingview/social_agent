"""Tool form actions and record choices, independent of Qt widgets."""
from __future__ import annotations

from pathlib import Path

from ..material_service import parse_links
from .selection_page import SelectionPage


class ToolController:
    def __init__(self, service, runner, tool, task_center):
        self.service, self.runner = service, runner
        self.tool, self.task_center = tool, task_center

    @staticmethod
    def read_links(path):
        path = Path(path)
        if path.stat().st_size > 5*1024*1024:
            raise ValueError('链接文件不能超过 5 MB')
        return path.read_text(encoding='utf-8-sig')

    def start(self, raw, options):
        options = dict(options)
        if self.tool == 'discover':
            source = options['source']
            if source == 'search':
                options['query'] = raw
            if source == 'user':
                options['user_key'] = raw
            items = [raw or 'timeline']
        elif self.tool == 'download':
            items, rejected = parse_links(raw)
            if rejected:
                raise ValueError('存在无效或频道级 URL，请改用具体帖子链接：\n'+'\n'.join(rejected[:5]))
        else:
            items = [line.strip() for line in raw.splitlines() if line.strip()]
        job_id = self.service.create(self.tool, items, options, trusted_local=True)
        self.runner.submit(job_id)
        return job_id

    def records(self, source, *, query='', limit=100):
        return self.record_page(source,query=query,limit=limit).entries

    def record_page(self, source, *, query='', limit=100, cursor=None):
        if source == 'library':
            offset = cursor or 0
            rows = self.service.library().list(query=query, limit=limit+1, offset=offset)
            options = [
                ('resource:'+row['id'], f'{Path(row["source_path"]).name} · {row["analysis_state"]}')
                for row in rows[:limit] if row['usage_state'] not in {'已删除','停用'}
                and row['analysis_state'] in {'未分析','分析失败','需复核'}
            ]
            return SelectionPage(options,offset+limit if len(rows)>limit else None)
        options = []
        seen = set()
        job_offset, artifact_offset = cursor or (0,0)
        # Bound work per page, including a page without matching media. A later
        # page remains reachable even when the recent jobs contain no results.
        jobs = self.task_center.list(tool='download', limit=50, offset=job_offset)
        for job_index,summary in enumerate(jobs, start=job_offset):
            job = self.task_center.get(summary['id'])
            artifacts = [artifact for result in job.get('results', {}).values()
                         for artifact in result.get('result', {}).get('artifacts', [])]
            start = artifact_offset if job_index==job_offset else 0
            for index in range(start,len(artifacts)):
                path = artifacts[index]['path']
                label = Path(path).name+' · '+job['id'][:8]
                if path not in seen and query.casefold() in (path+' '+label).casefold():
                    seen.add(path)
                    options.append((path,label))
                if len(options)>=limit:
                    return SelectionPage(options,(job_index,index+1))
        return SelectionPage(options,(job_offset+len(jobs),0) if len(jobs)==50 else None)

    def active_tasks(self):
        rows = []
        offset = 0
        while True:
            page = self.task_center.list(tool=self.tool, group='active', limit=100, offset=offset)
            rows.extend(page)
            if len(page) < 100:
                return rows
            offset += 100

    def stop(self, rows):
        for row in rows:
            if 'stop' in row.get('actions', ()):
                self.task_center.command(row['id'], 'stop')
