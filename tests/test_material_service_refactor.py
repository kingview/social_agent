"""Workflow facade contracts and crash/pause boundaries, without real plugins."""
from pathlib import Path
import sqlite3
import threading

import pytest

from social_ops_agent.diagnostics import current_context
from social_ops_agent.material_library import digest_file
from social_ops_agent.material_limits import model_slot
from social_ops_agent.material_service import MaterialService, parse_links, sidecar_metadata
from social_ops_agent.material_task_state import MaterialJobInterrupted
from social_ops_agent.material_workflows import analysis as analysis_workflow
from social_ops_agent.material_workflows import inputs


def make_service(tmp_path, response=None):
    calls = []

    class Invoker:
        async def call(self, name, arguments):
            calls.append((name, arguments, current_context()))
            if callable(response):
                return response(name, arguments)
            return response or {
                'confidence': .95,
                'material_features': {'quality': 85},
                'topics': ['科技'],
                'language': 'zh',
            }

    return MaterialService(
        tmp_path / 'out', tmp_path / 'state', invoker_factory=lambda settings: Invoker(),
    ), calls


def source_file(tmp_path, name='source.png'):
    path = tmp_path / name
    path.write_bytes(b'test-image')
    return path


def admit_source(service, source):
    return service.library().admit({
        'source_path': str(source), 'candidate_path': str(source),
        'passed': True, 'sha256': digest_file(source), 'media_type': 'image/png',
    })['resource_id']


def invoke(service, job):
    row = service.jobs.get(job)
    return service.handle(row['tool'], row['items'][0], row['parameters'], job)


def test_facade_imports_and_discovery_do_not_initialize_library(tmp_path):
    assert parse_links is inputs.parse_links
    assert sidecar_metadata is inputs.sidecar_metadata
    service, calls = make_service(tmp_path, {'completed': True, 'links': []})
    job = service.create('discover', ['https://t.me/public_channel'], {'max_items': 5}, execution_id='execution-a')
    assert invoke(service, job)['completed']
    name, arguments, diagnostics = calls[0]
    assert name == 'discover_public_materials'
    assert arguments['options'] == {'max_items': 5, 'start_url': 'https://t.me/public_channel'}
    assert diagnostics['task_id'] == job
    assert diagnostics['execution_id'] == 'execution-a'
    assert not (Path(service.settings().library_root) / 'resources.db').exists()


@pytest.mark.parametrize('session,tool', [(None, 'download_public_material'), ('session-1', 'download_media')])
def test_download_dispatch_export_and_original_preserved(tmp_path, session, tool):
    service, calls = make_service(tmp_path)
    service.output_root.mkdir()
    source = source_file(service.output_root)
    artifact = {'path': str(source), 'sha256': digest_file(source), 'size_bytes': source.stat().st_size}

    class Invoker:
        async def call(self, name, arguments):
            calls.append((name, arguments))
            return {'artifacts': [artifact], 'items': [{'text': '随附文本'}], 'completed': True}

    service.invoker_factory = lambda settings: Invoker()
    job = service.create('download', ['https://t.me/public_channel/12'], {'session_ref': session})
    result = invoke(service, job)
    assert calls[0][0] == tool
    assert source.exists()
    assert Path(result['artifacts'][0]['path']).read_bytes() == source.read_bytes()
    assert (Path(result['output_directory']) / 'text.txt').read_text() == '随附文本'


def test_download_rejects_plugin_output_outside_boundary(tmp_path):
    source = source_file(tmp_path)
    service, _ = make_service(tmp_path, {'artifacts': [{'path': str(source), 'sha256': digest_file(source)}]})
    job = service.create('download', ['https://t.me/public_channel/12'])
    with pytest.raises(ValueError, match='输出目录'):
        invoke(service, job)
    assert source.exists()


def test_paused_job_never_calls_plugin(tmp_path):
    service, calls = make_service(tmp_path)
    job = service.create('discover', ['https://t.me/public_channel'])
    service.jobs.command(job, 'pause')
    with pytest.raises(MaterialJobInterrupted) as error:
        invoke(service, job)
    assert error.value.command == 'pause'
    assert calls == []


def test_model_slot_wait_can_be_interrupted_and_releases_handles(tmp_path):
    waiting = threading.Event()
    cancelled = threading.Event()
    outcome = []
    root = tmp_path / 'slots'

    def check():
        waiting.set()
        if cancelled.is_set():
            raise MaterialJobInterrupted('stop')

    def worker():
        try:
            with model_slot(root, check_control=check):
                outcome.append('acquired')
        except MaterialJobInterrupted as error:
            outcome.append(error.command)

    with model_slot(root):
        thread = threading.Thread(target=worker)
        thread.start()
        assert waiting.wait(2)
        cancelled.set()
        thread.join(2)
        assert not thread.is_alive()
    assert outcome == ['stop']
    with model_slot(root, timeout=.1):
        pass


def test_successful_admission_replayed_without_reinspection(tmp_path):
    source = source_file(tmp_path)

    def inspect(name, arguments):
        path = Path(arguments['file_path'])
        return {'passed': True, 'source_path': str(path), 'candidate_path': str(path),
                'sha256': digest_file(path), 'media_type': 'image/png'}

    service, calls = make_service(tmp_path, inspect)
    job = service.create('import', [str(source)], trusted_local=True)
    first = invoke(service, job)
    replay = invoke(service, job)
    assert first['resource_id'] == replay['resource_id']
    assert len(calls) == 1
    assert len(service.library().list()) == 1


def test_analysis_commit_survives_report_failure_without_repeating_model(tmp_path, monkeypatch):
    service, calls = make_service(tmp_path)
    resource_id = admit_source(service, source_file(tmp_path))
    job = service.create('analyze', ['resource:' + resource_id])
    real_write = analysis_workflow.write_json

    def fail_write(*args):
        raise OSError('simulated full report volume')

    monkeypatch.setattr(analysis_workflow, 'write_json', fail_write)
    with pytest.raises(OSError, match='report volume'):
        invoke(service, job)
    assert service.library().get(resource_id)['analysis_state'] == '已分析'
    monkeypatch.setattr(analysis_workflow, 'write_json', real_write)
    replay = invoke(service, job)
    assert replay['resource_id'] == resource_id
    assert replay['analysis_state'] == '已分析'
    assert len(calls) == 1
    assert Path(replay['report_path']).is_file()


def test_local_analysis_report_replays_only_unchanged_source(tmp_path):
    service, calls = make_service(tmp_path)
    source = source_file(tmp_path)
    job = service.create('analyze', [str(source)], trusted_local=True)
    first = invoke(service, job)
    assert invoke(service, job) == first
    assert len(calls) == 1
    source.write_bytes(b'updated-image')
    changed = invoke(service, job)
    assert changed['report_path'] != first['report_path']
    assert len(calls) == 2


def test_service_recovery_uses_job_snapshot_library_not_current_settings(tmp_path):
    service, calls = make_service(tmp_path)
    library = service.library()
    resource_id = admit_source(service, source_file(tmp_path))
    job = service.create('analyze', ['resource:' + resource_id])
    service.jobs.claim(job)
    library.begin_analysis(resource_id, job_id=job)
    settings = service.settings()
    settings.library_root = str(tmp_path / 'other-library')
    settings.save(service.state_root)
    service.recover_interrupted()
    assert service.jobs.get(job)['state'] == '已暂停'
    assert library.get(resource_id)['analysis_state'] == '分析失败'
    assert calls == []


def test_analysis_interruption_is_not_saved_as_model_failure(tmp_path, monkeypatch):
    service, calls = make_service(tmp_path)
    resource_id = admit_source(service, source_file(tmp_path))
    job = service.create('analyze', ['resource:' + resource_id])

    def interrupt_stage(source):
        raise MaterialJobInterrupted('pause')

    monkeypatch.setattr(service, 'stage', interrupt_stage)
    with pytest.raises(MaterialJobInterrupted):
        invoke(service, job)
    resource = service.library().get(resource_id)
    assert resource['analysis_state'] == '分析失败'
    assert resource['analysis_job_id'] is None
    assert resource['analysis_json'] is None
    assert calls == []


def test_waiting_analysis_pauses_and_resumes_without_failed_checkpoint(tmp_path, monkeypatch):
    service, calls = make_service(tmp_path)
    resource_id = admit_source(service, source_file(tmp_path))
    job = service.create('analyze', ['resource:' + resource_id])
    staging = threading.Event()
    real_stage = service.stage

    def stage(source):
        result = real_stage(source)
        staging.set()
        return result

    monkeypatch.setattr(service, 'stage', stage)
    with model_slot(service.state_root / 'material-model-slots'):
        worker = threading.Thread(target=service.run_sync, args=(job,))
        worker.start()
        assert staging.wait(2)
        service.jobs.command(job, 'pause')
        worker.join(2)
        assert not worker.is_alive()
    paused = service.jobs.get(job)
    assert paused['state'] == '已暂停'
    assert paused['results'] == {}
    assert calls == []
    assert service.library().get(resource_id)['analysis_state'] == '分析失败'
    service.jobs.command(job, 'resume')
    assert service.run_sync(job)['state'] == '已完成'
    assert len(calls) == 1


def test_recovery_resets_only_explicit_unowned_legacy_resources(tmp_path):
    service, calls = make_service(tmp_path)
    library = service.library()
    assets = {}
    for name in ('legacy', 'unrelated', 'owned', 'leased', 'completed'):
        source = source_file(tmp_path, name + '.png')
        source.write_bytes(name.encode())
        assets[name] = admit_source(service, source)
    with library.db() as db:
        for name in ('legacy', 'unrelated', 'completed'):
            db.execute("UPDATE resources SET analysis_state='分析中' WHERE id=?", (assets[name],))
    library.begin_analysis(assets['owned'], job_id='other-task')
    library.begin_analysis(assets['leased'])
    job = service.create('analyze', [
        'resource:' + assets['legacy'], 'resource:' + assets['owned'],
        'resource:' + assets['leased'], 'resource:' + assets['completed'],
        library.get(assets['unrelated'])['path'],
    ])
    service.jobs.checkpoint(job, 3, {'status': 'completed', 'result': {'resource_id': assets['completed']}})
    service.jobs.claim(job)
    assert service.recover_interrupted() == [job]
    assert library.get(assets['legacy'])['analysis_state'] == '分析失败'
    for name in ('unrelated', 'owned', 'leased', 'completed'):
        assert library.get(assets[name])['analysis_state'] == '分析中'
    assert library.get(assets['owned'])['analysis_job_id'] == 'other-task'
    assert library.get(assets['leased'])['analysis_lease_id'] is not None
    assert calls == []


def test_legacy_recovery_preserves_resource_named_by_another_live_job(tmp_path):
    service, calls = make_service(tmp_path)
    resource_id = admit_source(service, source_file(tmp_path))
    library = service.library()
    with library.db() as db:
        db.execute("UPDATE resources SET analysis_state='分析中' WHERE id=?", (resource_id,))
    active = service.create('analyze', ['resource:' + resource_id])
    service.jobs.claim(active)
    queued = service.create('analyze', ['resource:' + resource_id])
    with service.jobs.execution_lock(active) as acquired:
        assert acquired
        assert service.recover_interrupted() == [queued]
        assert library.get(resource_id)['analysis_state'] == '分析中'
        assert service.jobs.get(active)['state'] == '执行中'
    assert service.recover_interrupted() == [active]
    assert library.get(resource_id)['analysis_state'] == '分析失败'
    assert calls == []


def test_old_database_migrates_and_interrupted_analysis_can_resume(tmp_path):
    service, calls = make_service(tmp_path)
    source = source_file(tmp_path)
    root = Path(service.settings().library_root)
    root.mkdir(parents=True)
    with sqlite3.connect(root / 'resources.db') as db:
        db.execute('''CREATE TABLE resources (
            id TEXT PRIMARY KEY, path TEXT, source_path TEXT, sha256 TEXT,
            source_sha256 TEXT, media_type TEXT, size_bytes INTEGER, phash TEXT,
            created_at TEXT, metadata_json TEXT, acquisition_state TEXT,
            intake_state TEXT, analysis_state TEXT, usage_state TEXT,
            analysis_json TEXT, features_json TEXT, scores_json TEXT,
            manual_subject_group TEXT)''')
        db.execute('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            'legacy-resource', str(source), str(source), digest_file(source),
            digest_file(source), 'image/png', source.stat().st_size, None,
            '2026-01-01', '{}', '不适用', '已入库', '分析中', '未使用',
            None, None, None, None,
        ))
    job = service.create('analyze', ['resource:legacy-resource'])
    service.jobs.claim(job)
    assert service.recover_interrupted() == [job]
    migrated = service.library().get('legacy-resource')
    assert migrated['analysis_state'] == '分析失败'
    assert migrated['analysis_job_id'] is None
    assert migrated['analysis_lease_id'] is None
    service.jobs.command(job, 'resume')
    assert service.run_sync(job)['state'] == '已完成'
    assert service.library().get('legacy-resource')['analysis_state'] == '已分析'
    assert len(calls) == 1
