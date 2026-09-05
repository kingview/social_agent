from datetime import date
import hashlib
import importlib.util
from pathlib import Path
from threading import Event

import pytest

from social_ops_agent.browser_lock_contract import workflow_key,operation_key,lock_paths
from social_ops_agent.browser_occupancy import window_busy,lock_busy
from social_ops_agent.browser_scheduler import BrowserTaskScheduler,WindowResource
from social_ops_agent.discovery_contract import DiscoveryInput,DISCOVERY_LIMITS,calendar_bounds
from social_ops_agent.material_dimensions import DIMENSIONS,FILTER_DIMENSIONS,SCORE_DIMENSIONS
from social_ops_agent.material_service import MaterialService
from social_ops_agent.material_settings import StrategyRule


def test_lock_identity_is_upgrade_compatible_and_keeps_two_namespaces(tmp_path):
    url='http://127.0.0.1:54345'
    key=workflow_key(url,'profile')
    assert key==workflow_key('http://localhost:54345','profile')
    assert key==workflow_key('http://[::1]:54345','profile')
    assert key==hashlib.sha256(b'loopback:54345|profile').hexdigest()
    assert operation_key(url,'profile')==hashlib.sha256(b'http://127.0.0.1:54345|profile').hexdigest()
    workflow,operation=lock_paths(tmp_path,url,'profile')
    assert workflow!=operation
    scheduler=BrowserTaskScheduler(workflow.parent)
    assert not window_busy(tmp_path,url,'profile')
    with scheduler.reserve([WindowResource(key,'测试窗口')],conversation_id='test',execution_id='test',cancelled=Event()):
        assert window_busy(tmp_path,url,'profile')
        assert not operation.exists()
    assert not window_busy(tmp_path,url,'profile')
    # The plugin operation lock independently marks occupancy.
    from social_ops_agent.browser_queue import open_lock
    from social_ops_agent.process_locks import _try_lock,_unlock
    operation.parent.mkdir()
    with open_lock(operation) as stream:
        assert _try_lock(stream)
        try: assert window_busy(tmp_path,url,'profile')
        finally: _unlock(stream)


@pytest.mark.parametrize('url',['http://localhost:bad','https://localhost:1','http://localhost/a','http://user@localhost','http://example.com','http://localhost?other=1'])
def test_nonlocal_or_ambiguous_lock_identity_rejected(url):
    with pytest.raises(ValueError): workflow_key(url,'profile')


def test_unreadable_lock_is_not_reported_as_idle():
    class Unreadable:
        def open(self,*args): raise PermissionError('fixture')
    assert lock_busy(Unreadable())


def test_discovery_defaults_limits_and_calendar_have_one_contract():
    assert DiscoveryInput(platform='xiaohongshu').source=='timeline'
    request=DiscoveryInput(platform='xiaohongshu',browser_engine='bitbrowser',session_ref='sess_xhs_'+'a'*24)
    assert request.execution_mode=='rpa' and request.sort=='top'
    assert DiscoveryInput(platform='telegram',start_url='https://t.me/public_channel').sort=='latest'
    assert calendar_bounds(date(2026,9,1),date(2026,9,2))['end_date'].startswith('2026-09-02T23:59:59.999999')
    for name,(low,high) in DISCOVERY_LIMITS.items():
        for value in (low,high): DiscoveryInput(platform='xiaohongshu',**{name:value})
        for value in (low-1,high+1):
            with pytest.raises(ValueError): DiscoveryInput(platform='xiaohongshu',**{name:value})


def test_invalid_harness_or_gui_input_never_creates_job(tmp_path):
    service=MaterialService(tmp_path/'out',tmp_path/'state')
    with pytest.raises(ValueError):
        service.create('discover',['https://t.me/public_channel'],{'platform':'telegram','browser_engine':'bitbrowser'})
    with pytest.raises(ValueError):
        service.create('discover',['https://t.me/public_channel'],{'platform':'telegram','timeout_seconds':1})
    assert service.jobs.list()==[]


def test_strategy_editor_and_validation_dimensions_match():
    assert {key for key,_ in DIMENSIONS}==SCORE_DIMENSIONS
    for key in FILTER_DIMENSIONS:
        StrategyRule(name=key,required={key:['example']},preferred={key:['example']},weights={key:1})
    with pytest.raises(ValueError): StrategyRule(name='bad',required={'quality':['example']})


def test_generated_contracts_are_current_when_sibling_tools_present(tmp_path):
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('sync_shared_contracts',root/'scripts/sync_shared_contracts.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    source=root/'src/social_ops_agent'
    tools=root.parent/'tools'
    if (tools/'social_content_crawler').exists():
        assert module.sync(source,tools,check=True)==[], 'Run scripts/sync_shared_contracts.py'
    target=tmp_path/'social_content_crawler/src/social_content_crawler'
    target.mkdir(parents=True); (target.parents[1]/'pyproject.toml').write_text('[project]')
    assert len(module.sync(source,tmp_path))==2
    assert module.sync(source,tmp_path,check=True)==[]
    (target/'discovery_contract.py').write_text('stale')
    assert module.sync(source,tmp_path,check=True)==[target/'discovery_contract.py']
