"""Shared entry point for manual toolbox jobs and Harness material calls.

The facade owns configuration and boundary validation. Individual workflows own
their stages; repositories own state transitions and transactional persistence.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from .material_jobs import MaterialJobs, MaterialRunner
from .material_library import MaterialLibrary, digest_file
from .material_settings import MaterialSettings
from .material_workflows import HANDLERS, WorkflowContext
from .material_workflows.inputs import parse_links, sidecar_metadata
from .plugins import PluginInvoker, PluginManager
from .material_preparation import MEDIA_SUFFIXES, expand_items
from .discovery_contract import DiscoveryInput
from .session_store import default_session_registry_path

TOOLS = {
    'discover': '链接发现', 'download': '资源下载',
    'import': '素材入库', 'analyze': '素材分析',
}

# Preserve the existing public import paths used by desktop and MCP callers.
__all__ = ['MaterialService', 'TOOLS', 'MEDIA_SUFFIXES', 'parse_links', 'sidecar_metadata']


class MaterialService:
    def __init__(
        self, output_root, state_root, *, registry_path=None,
        plugin_root=None, invoker_factory=None,
    ):
        self.output_root = Path(output_root).resolve()
        self.state_root = Path(state_root).resolve()
        self.registry_path = registry_path or default_session_registry_path()
        self.plugin_root = plugin_root
        self.jobs = MaterialJobs(self.state_root)
        self.invoker_factory = invoker_factory

    def settings(self):
        return MaterialSettings.load(self.state_root, self.output_root)

    def library(self):
        return MaterialLibrary(Path(self.settings().library_root))

    def create(
        self, tool, items, options=None, *, conversation_id=None,
        trusted_local=False, execution_id=None,
        check_cancel=None, on_progress=None, before_commit=None,
    ):
        if tool not in TOOLS:
            raise ValueError('未知素材工具')
        if not 1 <= len(items) <= 500:
            raise ValueError('每批需要 1..500 个项目')
        settings = self.settings()
        options = dict(options or {})
        if options.get('theme') and options['theme'] not in settings.themes:
            raise ValueError('主题必须从全局配置列表选择')
        if tool=='discover' and 'platform' in options:
            # Validate both GUI and Harness input using the same vendored
            # contract; the plugin independently validates again at its boundary.
            for item in items:
                DiscoveryInput.model_validate({**options,'start_url':str(item)})
        expanded = expand_items(tool,items,self.output_root,trusted_local=trusted_local,
                                check_cancel=check_cancel,on_progress=on_progress)
        if before_commit:
            before_commit()
        return self.jobs.create(
            tool, expanded,
            {'settings': settings.model_dump(mode='json'), 'options': options,
             'execution_id': execution_id},
            name=TOOLS[tool], conversation_id=conversation_id,
        )

    def invoker(self, settings):
        if self.invoker_factory:
            return self.invoker_factory(settings)
        return PluginInvoker(
            PluginManager(self.plugin_root), session_registry=self.registry_path,
            output_root=self.output_root, state_root=self.state_root,
            llm_base_url=settings.local_base_url, llm_model=settings.local_model,
            llm_api_key='local-model',
        )

    def stage(self, source):
        source = Path(source).resolve(strict=True)
        if source.is_relative_to(self.output_root):
            return source
        target = self.output_root / '.material-inputs' / digest_file(source) / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or digest_file(target) != digest_file(source):
            temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
            shutil.copy2(source, temporary)
            temporary.replace(target)
        return target

    def handle(self, tool, item, parameters, job_id):
        try:
            handler = HANDLERS[tool]
        except KeyError:
            raise ValueError('未知素材工具') from None
        context = WorkflowContext(self, parameters, job_id)
        context.check_control()
        return handler(context, item)

    def recover_interrupted(self):
        def recover_analysis(job):
            if job['tool'] != 'analyze':
                return
            settings = MaterialSettings.model_validate(job['parameters']['settings'])
            library = MaterialLibrary(Path(settings.library_root))
            library.recover_analysis_job(job['id'])
            library.recover_legacy_analysis(self._legacy_recovery_targets(job))

        return self.jobs.recover_interrupted(callback=recover_analysis)

    @staticmethod
    def _unfinished_resource_ids(job):
        return {
            str(item).removeprefix('resource:')
            for index, item in enumerate(job['items'])
            if str(item).startswith('resource:')
            and job['results'].get(str(index), {}).get('status') != 'completed'
        }

    def _legacy_recovery_targets(self, job):
        targets = self._unfinished_resource_ids(job)
        if not targets:
            return targets
        # A historical row has no owner field. If another live job explicitly
        # names it, preserve it rather than guessing which job started analysis.
        for other in self.jobs.list(tool='analyze', states={'执行中', '待执行'}):
            if other['id'] == job['id']:
                continue
            shared = targets.intersection(self._unfinished_resource_ids(other))
            if shared:
                with self.jobs.execution_lock(other['id']) as acquired:
                    if not acquired:
                        targets.difference_update(shared)
        return targets

    def run_sync(self, job_id):
        runner = MaterialRunner(self.jobs, self.handle, concurrency=1)
        try:
            return runner.run(job_id)
        finally:
            runner.close()

    def rescore(self):
        settings = self.settings()
        return MaterialLibrary(Path(settings.library_root)).rescore(settings.strategies)

    def confirm_review(self, resource_id, *, subject_group=None):
        self.library().review(resource_id, subject_group=subject_group)
        for job in self.jobs.list(tool='analyze'):
            for index, result in job['results'].items():
                if (
                    result.get('status') == 'review'
                    and result.get('result', {}).get('resource_id') == resource_id
                ):
                    result['status'] = 'completed'
                    result['result']['analysis_state'] = '已分析'
                    result['result']['reviewed_by'] = 'local-user'
                    self.jobs.checkpoint(job['id'], int(index), result)
            current = self.jobs.get(job['id'])
            if current['state'] == '待人工处理' and current['completed'] == current['total']:
                self.jobs.transition(job['id'], '已完成')
