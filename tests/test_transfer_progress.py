import json
import sys
import time
from pathlib import Path

import pytest

from social_ops_agent.transfer_progress import TransferProgressReader, format_transfer
from social_ops_agent.harness_client import HarnessJsonRpcClient, HarnessError


def test_changed_bytes_extend_activity_but_repeated_heartbeat_does_not(tmp_path):
    reader = TransferProgressReader(tmp_path, 'execution-123', lambda _:None)
    reader.path.parent.mkdir()
    item = dict(execution_id='execution-123', sequence=1, status='downloading',
        filename='file.mp4', downloaded_bytes=10, total_bytes=100)
    reader.path.write_text(json.dumps(item))
    assert reader.poll()
    item['sequence'] = 2
    reader.path.write_text(json.dumps(item))
    assert not reader.poll()
    item['downloaded_bytes'] = 20
    reader.path.write_text(json.dumps(item))
    assert reader.poll()


def test_stalled_transfer_times_out(tmp_path):
    reader = TransferProgressReader(tmp_path, 'execution-123', lambda _:None, stall_seconds=0)
    reader.path.parent.mkdir()
    reader.path.write_text(json.dumps(dict(execution_id='execution-123',sequence=1,status='downloading')))
    with pytest.raises(TimeoutError, match='没有新增数据'):
        reader.poll()


def test_transfer_display_includes_file_size_speed_and_count():
    text = format_transfer(dict(filename='video.mp4', post_index=2,post_total=10,
        downloaded_bytes=1048576,total_bytes=2097152,speed_bps=102400,files_completed=1))
    assert '2/10' in text and 'video.mp4' in text and '100 KiB/s' in text and '1 个媒体文件' in text


def test_other_execution_progress_is_ignored(tmp_path):
    reader = TransferProgressReader(tmp_path,'execution-123',lambda _:None)
    reader.path.parent.mkdir()
    reader.path.write_text(json.dumps({'execution_id':'other-execution','status':'downloading'}))
    assert not reader.poll()


@pytest.mark.parametrize('active', [True, False])
def test_harness_timeout_is_idle_not_total_when_transfer_progresses(tmp_path, active):
    client = HarnessJsonRpcClient(
        launch_args=[sys.executable,str(Path(__file__).parent/'fixtures/fake_harness_runtime.py')],
        cwd=tmp_path,env={'FAKE_HARNESS_DELAY':'.7'},timeout_seconds=.15)
    client.start(provider='fake',model='fake',max_tokens=256)
    client.activity_probe = (lambda: True) if active else (lambda: False)
    try:
        if active:
            assert client.run_turn(session_id='test',prompt='test').final_response
        else:
            with pytest.raises(HarnessError, match='超时'):
                client.run_turn(session_id='test',prompt='test')
    finally:
        client.close()
