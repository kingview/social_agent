import json
import pytest
from social_ops_agent.material_library import MaterialLibrary,digest_file


def test_composable_library_filters_paginate_after_filter(tmp_path):
    library=MaterialLibrary(tmp_path/'library')
    ids=[]
    for index in range(4):
        source=tmp_path/f'{index}.png'; source.write_bytes(str(index).encode())
        result=library.admit(dict(source_path=str(source),candidate_path=str(source),passed=True,
            sha256=digest_file(source),media_type='image/png' if index<3 else 'video/mp4',phash=None),metadata={'theme':'科技' if index<3 else '宠物'})
        ids.append(result['resource_id'])
        library.save_analysis(ids[-1],{'confidence':.9,'summary':'机器人介绍'},
            {'quality':60+index*10,'style':['科普']},[{'strategy':'主账号','recommendation':'建议使用' if index!=1 else '不建议'}])
    with library.db() as db:
        db.execute('UPDATE resources SET manual_subject_group=? WHERE id=?',('组一',ids[0]))
    assert len(library.list(query='科普'))==4
    assert len(library.list(query='机器人'))==4
    assert len(library.list(query='组一'))==1
    assert len(library.list(media_type='video'))==1
    rows=library.list(theme='科技',minimum_quality=70,strategy='主账号')
    assert [r['id'] for r in rows]==[ids[2]]
    assert library.list(theme='科技',minimum_quality=70,strategy='主账号',offset=1,limit=1)==[]
    assert library.list(theme="科技' OR 1=1 --")==[]
    assert library.list(subject_group='组一')[0]['id']==ids[0]
    for value in (True,float('nan'),101,-1):
        with pytest.raises(ValueError): library.list(minimum_quality=value)
    with pytest.raises(ValueError): library.list(media_type='document')


def test_harness_library_filters_are_bounded_and_forwarded(tmp_path,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from social_ops_agent import mcp_server
    calls=[]
    monkeypatch.setattr(mcp_server,'runtime',lambda:SimpleNamespace(output_root=tmp_path/'out',state_root=tmp_path/'state'))
    def listing(self,**options):
        calls.append(options)
        return [{'id':str(i)} for i in range(3)]
    monkeypatch.setattr(MaterialLibrary,'list',listing)
    result=asyncio.run(mcp_server.list_material_library(media_type='video',theme='科技',minimum_quality=80,strategy='主账号',limit=2,offset=10))
    assert result['next_offset']==12 and len(result['resources'])==2
    assert calls[-1]['limit']==3 and calls[-1]['offset']==10
    assert calls[-1]['strategy']=='主账号' and calls[-1]['minimum_quality']==80
    for options in ({'limit':0},{'limit':501},{'offset':-1}):
        with pytest.raises(ValueError): asyncio.run(mcp_server.list_material_library(**options))
