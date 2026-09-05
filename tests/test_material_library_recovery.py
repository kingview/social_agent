"""Isolated repository regressions: no live models or user databases."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from social_ops_agent import material_library as module
from social_ops_agent.material_library import MaterialLibrary, digest_file
from social_ops_agent.material_settings import StrategyRule


def report(source, **changes):
    return {'source_path': str(source), 'candidate_path': str(source), 'passed': True,
            'sha256': digest_file(source), 'media_type': 'image/png', 'phash': None, **changes}


def asset(library, directory, name='original.png'):
    source = directory / name
    source.write_bytes(name.encode())
    result = library.admit(report(source))
    return result['resource_id']


def test_reads_do_not_reserve_writer_lock(tmp_path):
    library = MaterialLibrary(tmp_path / 'library')
    resource_id = asset(library, tmp_path)
    writer = sqlite3.connect(library.path)
    writer.execute('BEGIN IMMEDIATE')
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: (MaterialLibrary(library.root).get(resource_id), library.list(), library.attempts()))
            assert future.result(timeout=1)[0]['id'] == resource_id
    finally:
        writer.rollback()
        writer.close()
    with library.read_db() as reader:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            reader.execute("UPDATE resources SET usage_state='已删除'")


def test_hash_and_copy_do_not_hold_writer_transaction(tmp_path, monkeypatch):
    library = MaterialLibrary(tmp_path / 'library')
    source = tmp_path / 'source.png'
    source.write_bytes(b'original')
    inspection = report(source)
    original_digest, original_copy = module.digest_file, module.shutil.copy2
    observations = []

    def assert_writer_available():
        with sqlite3.connect(library.path, timeout=.05) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.rollback()
        observations.append(True)

    def digest(path):
        assert_writer_available()
        return original_digest(path)

    def copy(source_path, destination):
        assert_writer_available()
        return original_copy(source_path, destination)

    monkeypatch.setattr(module, 'digest_file', digest)
    monkeypatch.setattr(module.shutil, 'copy2', copy)
    admitted = library.admit(inspection, task_id='task')
    assert admitted['intake_state'] == '已入库'
    assert len(observations) >= 3
    assert source.read_bytes() == b'original'


def test_known_duplicate_does_not_copy_again(tmp_path, monkeypatch):
    library = MaterialLibrary(tmp_path / 'library')
    source = tmp_path / 'source.png'
    source.write_bytes(b'original')
    inspection = report(source)
    library.admit(inspection, task_id='first-job')
    monkeypatch.setattr(module.shutil, 'copy2', lambda *args: pytest.fail('duplicate must not be copied'))
    assert library.admit(inspection, task_id='second-job')['issues'] == ['重复']


@pytest.mark.parametrize('same_job', [False, True])
def test_concurrent_admission_rechecks_identity_and_cleans_only_loser_copy(tmp_path, monkeypatch, same_job):
    library = MaterialLibrary(tmp_path / 'library')
    source = tmp_path / 'source.png'
    source.write_bytes(b'original')
    inspection = report(source)
    copied = threading.Barrier(2)
    original_copy = module.shutil.copy2

    def copy(source_path, destination):
        result = original_copy(source_path, destination)
        copied.wait(timeout=3)
        return result

    monkeypatch.setattr(module.shutil, 'copy2', copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(library.admit, inspection, task_id='first' if same_job else str(index))
                   for index in range(2)]
        results = [future.result(timeout=5) for future in futures]
    rows = library.list()
    assert len(rows) == 1
    assert len(list(library.root.rglob('01.png'))) == 1
    assert not list(library.root.rglob('*.part'))
    assert source.read_bytes() == b'original'
    if same_job:
        assert results[0] == results[1]
        assert len(library.attempts(failed_only=False)) == 1
    else:
        assert sorted(result['intake_state'] for result in results) == ['已入库', '未通过']
        assert next(result for result in results if 'duplicate_of' in result)['duplicate_of'] == rows[0]['id']


@pytest.mark.parametrize('internal', [False, True])
def test_transaction_failure_rolls_back_asset_attempt_and_only_generated_files(tmp_path, monkeypatch, internal):
    library = MaterialLibrary(tmp_path / 'library')
    source = (library.root if internal else tmp_path) / 'original.png'
    source.write_bytes(b'original')

    def fail(*args, **kwargs):
        raise RuntimeError('simulated checkpoint persistence failure')

    monkeypatch.setattr(library, '_record_attempt', fail)
    with pytest.raises(RuntimeError, match='persistence failure'):
        library.admit(report(source), task_id='task')
    assert library.list() == []
    assert library.attempts(failed_only=False) == []
    assert source.read_bytes() == b'original'
    assert not list(library.root.rglob('01.png'))
    assert not list(library.root.rglob('*.part'))
    with library.read_db() as db:
        assert db.execute('SELECT COUNT(*) FROM admission_receipts').fetchone()[0] == 0


def test_copy_failure_removes_partial_but_never_original(tmp_path, monkeypatch):
    library = MaterialLibrary(tmp_path / 'library')
    source = tmp_path / 'original.png'
    source.write_bytes(b'original')

    def copy(_source, destination):
        Path(destination).write_bytes(b'partial')
        raise OSError('disk full')

    monkeypatch.setattr(module.shutil, 'copy2', copy)
    with pytest.raises(OSError, match='disk full'):
        library.admit(report(source))
    assert source.read_bytes() == b'original'
    assert not list(library.root.rglob('*.part'))
    assert not library.list()


def test_success_receipt_survives_process_exit_before_job_checkpoint(tmp_path):
    source = tmp_path / 'original.png'
    source.write_bytes(b'original')
    root = tmp_path / 'library'
    code = '''
import os, sys
from pathlib import Path
from social_ops_agent.material_library import MaterialLibrary, digest_file
source = Path(sys.argv[1])
library = MaterialLibrary(Path(sys.argv[2]))
library.admit({'source_path': str(source), 'candidate_path': str(source), 'passed': True,
               'sha256': digest_file(source), 'media_type': 'image/png'}, task_id='job-before-crash')
os._exit(23)
'''
    result = subprocess.run([sys.executable, '-c', code, str(source), str(root)], check=False)
    assert result.returncode == 23
    library = MaterialLibrary(root)
    recovered = library.admission_receipt('job-before-crash', source)
    assert recovered['resource_id'] == library.list()[0]['id']
    assert library.admit(report(source), task_id='job-before-crash') == recovered
    assert len(library.attempts(failed_only=False)) == 1
    assert library.admit(report(source), task_id='different-job')['issues'] == ['重复']


def test_admission_receipt_checks_original_and_saved_copy(tmp_path):
    library = MaterialLibrary(tmp_path / 'library')
    source = tmp_path / 'original.png'
    source.write_bytes(b'original')
    admitted = library.admit(report(source), task_id='job')
    source.write_bytes(b'edited source')
    assert library.admission_receipt('job', source) is None
    source.write_bytes(b'original')
    Path(admitted['path']).write_bytes(b'corrupted library copy')
    with pytest.raises(ValueError, match='丢失或发生变化'):
        library.admission_receipt('job', source)


def test_analysis_recovery_is_owned_and_completed_result_replays(tmp_path):
    library = MaterialLibrary(tmp_path / 'library')
    interrupted = asset(library, tmp_path, 'interrupted.png')
    other = asset(library, tmp_path, 'other.png')
    completed = asset(library, tmp_path, 'completed.png')
    interrupted_lease = library.begin_analysis(interrupted, job_id='job-a')['lease_id']
    library.begin_analysis(other, job_id='job-b')
    completed_lease = library.begin_analysis(completed, job_id='job-a')['lease_id']
    analysis, features, scores = {'confidence': .9}, {'quality': 80}, [{'status': '待配置'}]
    library.save_analysis(completed, analysis, features, scores, job_id='job-a', lease_id=completed_lease)
    assert library.recover_analysis_job('job-a') == 1
    assert library.get(interrupted)['analysis_state'] == '分析失败'
    assert library.get(other)['analysis_state'] == '分析中'
    assert library.get(completed)['analysis_state'] == '已分析'
    replay = library.begin_analysis(completed, job_id='job-a')
    assert replay == {'replayed': True, 'lease_id': None, 'result': {
        'analysis_state': '已分析', 'analysis': analysis, 'features': features, 'scores': scores}}
    assert library.analysis_receipt(completed, 'job-a') == replay['result']
    assert library.analysis_receipt(completed, 'job-b') is None
    with pytest.raises(ValueError, match='所有权'):
        library.save_analysis(interrupted, analysis, features, scores, job_id='job-a', lease_id=interrupted_lease)


def test_stale_same_job_lease_cannot_overwrite_restarted_analysis(tmp_path):
    library = MaterialLibrary(tmp_path / 'library')
    resource_id = asset(library, tmp_path)
    first = library.begin_analysis(resource_id, job_id='job')
    with pytest.raises(ValueError, match='已有分析任务'):
        library.begin_analysis(resource_id, job_id='different-job')
    library.recover_analysis_job('job')
    second = library.begin_analysis(resource_id, job_id='job')
    assert first['lease_id'] != second['lease_id']
    with pytest.raises(ValueError, match='租约已失效'):
        library.save_analysis(resource_id, {'confidence': .9}, {}, [], job_id='job', lease_id=first['lease_id'])
    assert library.get(resource_id)['analysis_state'] == '分析中'
    library.save_analysis(resource_id, {'confidence': .5}, {}, [], job_id='job', lease_id=second['lease_id'])
    with pytest.raises(ValueError, match='不再持有'):
        library.save_analysis(resource_id, {'error': 'old result'}, {}, [], failed=True, job_id='job', lease_id=first['lease_id'])
    assert library.analysis_receipt(resource_id, 'job')['analysis_state'] == '需复核'


def test_legacy_schema_migrates_and_backfills_admission_receipts(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    source = root / 'original.png'
    source.write_bytes(b'legacy')
    with sqlite3.connect(root / 'resources.db') as db:
        db.execute('''CREATE TABLE resources (
            id TEXT PRIMARY KEY,path TEXT NOT NULL,source_path TEXT NOT NULL,sha256 TEXT UNIQUE NOT NULL,
            source_sha256 TEXT NOT NULL,media_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,phash TEXT,
            created_at TEXT NOT NULL,metadata_json TEXT NOT NULL,acquisition_state TEXT NOT NULL,
            intake_state TEXT NOT NULL,analysis_state TEXT NOT NULL,usage_state TEXT NOT NULL,
            analysis_json TEXT,features_json TEXT,scores_json TEXT,manual_subject_group TEXT)''')
        db.execute('''CREATE TABLE intake_attempts (id TEXT PRIMARY KEY,task_id TEXT,source_path TEXT NOT NULL,
            state TEXT NOT NULL,issues_json TEXT NOT NULL,resource_id TEXT,created_at TEXT NOT NULL)''')
        db.execute('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('legacy-id', str(source), str(source), digest_file(source), digest_file(source),
                    'image/png', 6, None, '2026-01-01', '{}', '不适用', '已入库', '未分析', '未使用',
                    None, None, None, None))
        db.execute('INSERT INTO intake_attempts VALUES(?,?,?,?,?,?,?)',
                   ('attempt', 'legacy-job', str(source), '已入库', '[]', 'legacy-id', '2026-01-01'))
    library = MaterialLibrary(root)
    assert library.admission_receipt('legacy-job', source)['resource_id'] == 'legacy-id'
    assert library.get('legacy-id')['analysis_lease_id'] is None
    library.begin_analysis('legacy-id')
    assert library.save_analysis('legacy-id', {'confidence': .9}, {}, []) == '已分析'
    reopened = MaterialLibrary(root)
    assert len(reopened.list()) == 1
    assert reopened.get('legacy-id')['analysis_state'] == '已分析'


def test_rescore_does_not_overwrite_changed_analysis(tmp_path, monkeypatch):
    library = MaterialLibrary(tmp_path / 'library')
    resource_id = asset(library, tmp_path)
    library.save_analysis(resource_id, {'confidence': .9}, {'quality': 80}, [{'old': True}])
    rule = StrategyRule(name='quality', weights={'quality': 1})
    original = module.strategy_scores

    def changing_score(features, rules):
        library.save_analysis(resource_id, {'confidence': .9}, {'quality': 20}, [{'new': True}])
        return original(features, rules)

    monkeypatch.setattr(module, 'strategy_scores', changing_score)
    assert library.rescore([rule]) == 0
    assert json.loads(library.get(resource_id)['scores_json']) == [{'new': True}]


def test_paged_reads_preserve_default_shape_and_order(tmp_path):
    library = MaterialLibrary(tmp_path / 'library')
    for index in range(4):
        asset(library, tmp_path, f'{index}.png')
    complete = library.list()
    assert library.list(limit=2, offset=1) == complete[1:3]
    assert library.list(offset=2) == complete[2:]
    attempts = library.attempts(failed_only=False)
    assert library.attempts(failed_only=False, limit=1, offset=2) == attempts[2:3]
    assert library.list(limit=0) == []
    with pytest.raises(ValueError, match='非负整数'):
        library.list(offset=-1)
