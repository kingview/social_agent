import json
import threading
from pathlib import Path

import pytest

from social_ops_agent.material_jobs import MaterialJobs, MaterialRunner
from social_ops_agent.material_library import MaterialLibrary, digest_file, strategy_scores
from social_ops_agent.material_settings import MaterialSettings, StrategyRule
from social_ops_agent.material_service import MaterialService, parse_links


def inspection(path, **changes):
    return {'source_path':str(path),'candidate_path':str(path),'passed':True,
        'sha256':digest_file(path),'media_type':'image/png','phash':None, **changes}


def test_admission_preserves_original_and_rejects_duplicate(tmp_path):
    source=tmp_path/'original.png'; source.write_bytes(b'original')
    library=MaterialLibrary(tmp_path/'library')
    rejected=library.admit(inspection(source,passed=False,issues=['模糊']))
    assert rejected['intake_state']=='未通过' and library.list()==[]
    result=library.admit(inspection(source),metadata={'platform':'telegram'})
    assert source.read_bytes()==b'original'
    assert Path(result['path']).read_bytes()==b'original'
    assert 'telegram' in Path(result['path']).parts
    assert library.admit(inspection(source))['issues']==['重复']
    assert len(library.list())==1
    with library.db() as db:
        assert db.execute('SELECT COUNT(*) FROM intake_attempts').fetchone()[0]==3


def test_changed_candidate_not_admitted(tmp_path):
    source=tmp_path/'original.png'; source.write_bytes(b'original')
    report=inspection(source); source.write_bytes(b'changed')
    library=MaterialLibrary(tmp_path/'library')
    with pytest.raises(ValueError,match='发生变化'):
        library.admit(report)
    assert not library.list()


def test_library_file_not_copied_and_review_audited(tmp_path):
    library=MaterialLibrary(tmp_path/'library')
    source=library.root/'existing.png'; source.write_bytes(b'original')
    result=library.admit(inspection(source))
    assert result['path']==str(source)
    library.begin_analysis(result['resource_id'])
    with pytest.raises(ValueError): library.begin_analysis(result['resource_id'])
    assert library.save_analysis(result['resource_id'],{'confidence':.4},{},[])=='需复核'
    library.review(result['resource_id'],subject_group='人工组一')
    assert library.get(result['resource_id'])['analysis_state']=='已分析'
    library.set_usage(result['resource_id'],'已删除')
    assert source.exists()


def test_strategy_rules_deterministic_missing_and_excluded():
    rule=StrategyRule(name='科技',weights={'quality':1,'topic':1},required={'language':['zh']},preferred={'topic':['科技']})
    features={'quality':80,'language':['zh'],'topic':['科技']}
    score=strategy_scores(features,[rule])[0]
    assert score['score']==90 and score['recommendation']=='建议使用'
    assert strategy_scores({**features,'language':['en']},[rule])[0]['recommendation']=='排除'
    assert strategy_scores({'language':['zh']},[rule])[0]['recommendation']=='待复核'
    assert strategy_scores({},[])==[{'status':'待配置'}]
    with pytest.raises(ValueError): StrategyRule(name='bad',weights={'quality':0})


def test_settings_local_model_and_snapshot(tmp_path):
    settings=MaterialSettings.load(tmp_path,tmp_path/'out')
    with pytest.raises(ValueError): settings.model_validate({**settings.model_dump(),'local_base_url':'https://example.com/v1'})
    settings.save(tmp_path)
    jobs=MaterialJobs(tmp_path)
    job=jobs.create('analyze',['a'],settings.model_dump())
    settings.local_model='changed'; settings.save(tmp_path)
    assert jobs.get(job)['parameters']['local_model']!='changed'


def test_pause_retry_skip_success_and_recover(tmp_path):
    jobs=MaterialJobs(tmp_path)
    job=jobs.create('import',['one','two','three'],{})
    called=[]
    def handler(tool,item,parameters,job_id):
        called.append(item)
        if item=='one': jobs.command(job_id,'pause')
        if item=='two' and called.count('two')==1: raise RuntimeError('broken')
        return {'completed':True}
    runner=MaterialRunner(jobs,handler)
    runner._run(job)
    assert jobs.get(job)['state']=='已暂停'
    assert called==['one']
    jobs.command(job,'resume'); runner._run(job)
    assert jobs.get(job)['state']=='部分完成'
    jobs.command(job,'retry'); runner._run(job)
    assert called==['one','two','three','two']
    assert jobs.get(job)['state']=='已完成'
    second=jobs.create('import',['a'],{}); assert jobs.claim(second)
    assert not jobs.claim(second)
    jobs.recover_interrupted()
    assert jobs.get(second)['state']=='已暂停'
    runner.close()


def test_invalid_urls_dedup_and_no_channel_download():
    urls,rejected=parse_links('https://t.me/public_channel/123 https://t.me/public_channel/123?single https://t.me/public_channel http://x.com/a/status/1 https://localhost/private')
    assert urls==['https://t.me/public_channel/123']
    assert len(rejected)==3


def test_recovery_does_not_interrupt_a_live_worker(tmp_path):
    jobs=MaterialJobs(tmp_path)
    job=jobs.create('import',['one'],{})
    entered,release=threading.Event(),threading.Event()
    def handler(*args):
        entered.set()
        assert release.wait(3)
        return {'completed':True}
    runner=MaterialRunner(jobs,handler)
    runner.submit(job)
    try:
        assert entered.wait(3)
        jobs.recover_interrupted()
        assert jobs.get(job)['state']=='执行中'
        another=MaterialRunner(jobs,lambda *args:pytest.fail('duplicate execution'))
        another._run(job)
        another.close()
    finally:
        release.set()
        runner.pool.shutdown(wait=True)
    assert jobs.get(job)['state']=='已完成'


def test_review_resolves_waiting_job_and_keeps_audit(tmp_path):
    service=MaterialService(tmp_path/'out',tmp_path/'state')
    source=tmp_path/'source.png'; source.write_bytes(b'image')
    library=service.library()
    asset=library.admit(inspection(source))['resource_id']
    library.save_analysis(asset,{'confidence':.5},{},[])
    job=service.create('analyze',['resource:'+asset])
    service.jobs.checkpoint(job,0,{'status':'review','result':{'resource_id':asset,'analysis_state':'需复核'}})
    service.jobs.transition(job,'待人工处理')
    service.confirm_review(asset,subject_group='人工分组')
    assert service.jobs.get(job)['state']=='已完成'
    assert library.get(asset)['manual_subject_group']=='人工分组'


def test_service_import_analyze_with_stub_plugin(tmp_path):
    source=tmp_path/'outside.png'; source.write_bytes(b'bytes')
    calls=[]
    class Invoker:
        async def call(self,name,args):
            calls.append((name,args))
            if name=='inspect_material': return inspection(Path(args['file_path']))
            return {'confidence':.95,'material_features':{'quality':80},'topics':['科技'],'language':'zh'}
    service=MaterialService(tmp_path/'out',tmp_path/'state',invoker_factory=lambda settings:Invoker())
    with pytest.raises(ValueError,match='输出目录'): service.create('import',[str(source)])
    job=service.create('import',[str(source)],trusted_local=True)
    result=service.run_sync(job)
    assert result['state']=='已完成'
    asset=result['results']['0']['result']['resource_id']
    analysis=service.create('analyze',['resource:'+asset])
    analyzed=service.run_sync(analysis)
    assert analyzed['state']=='已完成'
    assert service.library().get(asset)['analysis_state']=='已分析'
    assert source.read_bytes()==b'bytes'
    assert 'analysis_profile' in calls[-1][1]


def test_semantic_fallback_is_failure_not_success(tmp_path):
    source=tmp_path/'out'/'a.png'; source.parent.mkdir(); source.write_bytes(b'bytes')
    class Invoker:
        async def call(self,*args): return {'warnings':['Semantic model failed; fallback used'],'confidence':.9}
    service=MaterialService(source.parent,tmp_path/'state',invoker_factory=lambda settings:Invoker())
    job=service.create('analyze',[str(source)])
    assert service.run_sync(job)['state']=='执行失败'


def test_settings_theme_validation_and_batch_expansion(tmp_path):
    source=tmp_path/'source'; source.mkdir()
    (source/'a.png').write_bytes(b'a'); (source/'a.txt').write_text('not media')
    service=MaterialService(tmp_path/'out',tmp_path/'state')
    with pytest.raises(ValueError,match='主题'): service.create('import',[str(source)],{'theme':'不存在'},trusted_local=True)
    job=service.create('import',[str(source)],trusted_local=True)
    assert service.jobs.get(job)['items']==[str(source/'a.png')]
