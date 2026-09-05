"""Task-ledger and runner coordination at deterministic concurrency boundaries."""
import sqlite3
import threading

import pytest

from social_ops_agent.material_jobs import MaterialJobs, MaterialRunner
from social_ops_agent.material_task_state import MaterialJobInterrupted, material_actions


def job_store(tmp_path):
    jobs = MaterialJobs(tmp_path)
    return jobs, jobs.create('analyze', ['first', 'second'], {})


@pytest.mark.parametrize('command,state', [('pause', '已暂停'), ('stop', '已停止')])
@pytest.mark.parametrize('running', [True, False])
def test_pause_stop_acknowledged_commands_are_resumable(tmp_path, command, state, running):
    jobs, job = job_store(tmp_path)
    if running:
        jobs.claim(job)
    jobs.command(job, command)
    if running:
        assert jobs.get(job)['command'] == command
        with pytest.raises(MaterialJobInterrupted):
            jobs.check_control(job)
        jobs.transition(job, state)
    row = jobs.get(job)
    assert row['state'] == state
    assert row['command'] == ''
    assert material_actions(row) == ['resume', 'retry']
    jobs.command(job, 'resume')
    assert jobs.claim(job)


def test_legacy_pause_command_keeps_resume_action():
    assert material_actions({'state': '已暂停', 'command': 'pause'}) == ['resume', 'retry']
    assert material_actions({'state': '执行中', 'command': 'pause'}) == []


def test_stop_wins_over_pause_ack_race(tmp_path):
    jobs, job = job_store(tmp_path)
    jobs.claim(job)
    jobs.command(job, 'pause')
    jobs.command(job, 'stop')
    with pytest.raises(ValueError, match='正在停止'):
        jobs.command(job, 'pause')
    jobs.transition(job, '已暂停')
    assert jobs.get(job)['state'] == '已停止'
    assert jobs.get(job)['command'] == ''


def test_resume_during_old_worker_cleanup_is_not_lost(tmp_path, monkeypatch):
    jobs, job = job_store(tmp_path)
    paused, release_tail, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    transition = jobs.transition

    def delayed_transition(job_id, state, **kwargs):
        transition(job_id, state, **kwargs)
        if state == '已暂停':
            paused.set()
            assert release_tail.wait(3)
        elif state == '已完成':
            finished.set()

    def handler(tool, item, parameters, job_id):
        calls.append(item)
        if item == 'first':
            jobs.command(job_id, 'pause')
        return {'completed': True}

    monkeypatch.setattr(jobs, 'transition', delayed_transition)
    runner = MaterialRunner(jobs, handler, concurrency=1)
    runner.submit(job)
    try:
        assert paused.wait(3)
        assert job in runner.active
        jobs.command(job, 'resume')
        runner.submit(job)
        release_tail.set()
        assert finished.wait(3)
        assert jobs.get(job)['state'] == '已完成'
        assert calls == ['first', 'second']
    finally:
        release_tail.set()
        runner.close()
        runner.pool.shutdown(wait=True)


def test_resume_from_another_runner_waits_for_releasing_owner(tmp_path):
    jobs, job = job_store(tmp_path)
    done = threading.Event()
    calls = []

    def handler(tool, item, parameters, job_id):
        calls.append(item)
        if item == 'second':
            done.set()
        return {'completed': True}

    runner = MaterialRunner(jobs, handler)
    try:
        with jobs.execution_lock(job) as acquired:
            assert acquired
            jobs.claim(job)
            jobs.checkpoint(job, 0, {'status': 'completed', 'result': {'completed': True}})
            jobs.transition(job, '已暂停')
            jobs.command(job, 'resume')
            runner.submit(job)
            assert not done.wait(.1)
            assert job in runner.active
        assert done.wait(3)
    finally:
        runner.pool.shutdown(wait=True)
        runner.close()
    assert calls == ['second']
    assert jobs.get(job)['state'] == '已完成'


def test_pause_interrupt_is_not_a_failed_item(tmp_path):
    jobs, job = job_store(tmp_path)
    calls = []

    def handler(tool, item, parameters, job_id):
        calls.append(item)
        if len(calls) == 1:
            jobs.command(job_id, 'pause')
            jobs.check_control(job_id)
        return {'completed': True}

    runner = MaterialRunner(jobs, handler)
    try:
        paused = runner.run(job)
        assert paused['state'] == '已暂停'
        assert paused['results'] == {}
        assert paused['error'] == ''
        jobs.command(job, 'resume')
        assert runner.run(job)['state'] == '已完成'
        assert calls == ['first', 'first', 'second']
    finally:
        runner.close()


def test_recovery_callback_holds_lock_preserves_completed_and_observes_stop(tmp_path):
    jobs, job = job_store(tmp_path)
    jobs.claim(job)
    receipt = {'status': 'completed', 'result': {'artifact': 'already-produced'}}
    jobs.checkpoint(job, 0, receipt)
    seen = []

    def recovery(row):
        with jobs.execution_lock(row['id']) as acquired:
            assert not acquired
        seen.append(row['results'])
        jobs.command(job, 'stop')

    assert jobs.recover_interrupted(callback=recovery) == [job]
    row = jobs.get(job)
    assert row['state'] == '已停止'
    assert row['command'] == ''
    assert row['results'] == {'0': receipt}
    assert seen == [{'0': receipt}]
    assert jobs.recover_interrupted(callback=recovery) == []


def test_recovery_hook_failure_leaves_job_retryable_for_next_recovery(tmp_path):
    jobs, job = job_store(tmp_path)
    jobs.claim(job)

    def failure(row):
        raise OSError('library unavailable')

    with pytest.raises(OSError, match='library unavailable'):
        jobs.recover_interrupted(callback=failure)
    assert jobs.get(job)['state'] == '执行中'
    assert jobs.recover_interrupted() == [job]


def test_read_queries_and_existing_constructor_do_not_reserve_writer(tmp_path):
    jobs, job = job_store(tmp_path)
    errors = []
    finished = threading.Event()
    writer = sqlite3.connect(jobs.path, timeout=.1)
    writer.execute('BEGIN IMMEDIATE')

    def reads():
        try:
            reopened = MaterialJobs(tmp_path)
            assert reopened.get(job)['id'] == job
            assert reopened.list(summary=True, limit=1)[0]['id'] == job
            reopened.check_control(job)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    reader = threading.Thread(target=reads)
    reader.start()
    try:
        assert finished.wait(2), 'Read path waited for a writer reservation'
    finally:
        writer.rollback()
        writer.close()
        reader.join(3)
    assert errors == []


def test_runner_close_rejects_new_work_and_retains_paused_result(tmp_path):
    jobs, job = job_store(tmp_path)
    runner = MaterialRunner(jobs, lambda *args: {'completed': True})
    runner.close()
    with pytest.raises(RuntimeError, match='已关闭'):
        runner.submit(job)
    with pytest.raises(RuntimeError, match='已关闭'):
        runner.run(job)
    assert not runner.active
