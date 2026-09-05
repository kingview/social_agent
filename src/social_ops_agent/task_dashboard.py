"""Read-only, paged projections of the existing authoritative Agent journal."""
import json


_BASE = '''WITH documents AS (
    SELECT task_id,conversation_id,message,state,run_id,created_at,updated_at,
           revision,publish_attempted,publish_state,
           CASE WHEN json_valid(execution_json) THEN execution_json ELSE '{}' END AS execution_json,
           CASE WHEN json_valid(plan_json) THEN plan_json ELSE '{}' END AS plan_json,
           CASE WHEN json_valid(legacy_json) THEN legacy_json ELSE '{}' END AS legacy_json
    FROM tasks
), projected AS (
    SELECT task_id,conversation_id,message,created_at,updated_at,revision,publish_attempted,publish_state,legacy_json,
      CASE WHEN json_type(execution_json)='object' AND execution_json!='{}' THEN execution_json
           WHEN json_type(legacy_json,'$.result')='object' THEN json_extract(legacy_json,'$.result')
           ELSE '{}' END AS report,
      CASE WHEN json_type(plan_json)='object' AND plan_json!='{}' THEN plan_json
           WHEN json_type(legacy_json,'$.plan')='object' THEN json_extract(legacy_json,'$.plan')
           ELSE '{}' END AS plan,
      CASE WHEN run_id IS NOT NULL OR (json_type(plan_json)='object' AND plan_json!='{}') THEN state
           ELSE COALESCE(json_extract(legacy_json,'$.status'),state) END AS raw_state
    FROM documents
), normalized AS (
    SELECT *, CASE
      WHEN json_extract(report,'$.cancelled')=1 THEN '已停止'
      WHEN raw_state IN ('succeeded','completed') AND json_extract(report,'$.completion_status')='partial' THEN '部分完成'
      WHEN raw_state IN ('succeeded','completed') THEN '已完成'
      WHEN raw_state IN ('planning','executing') THEN '执行中'
      WHEN raw_state='planned' THEN '待执行'
      WHEN raw_state='partial' THEN '部分完成'
      WHEN raw_state='failed' THEN '执行失败'
      WHEN raw_state IN ('cancelled','stopped') THEN '已停止'
      WHEN raw_state IN ('interrupted','paused') THEN '已暂停'
      ELSE '待人工处理' END AS display_state
    FROM projected
) '''


def query_tasks(store, *, query='', states=None, limit=500, offset=0, summary=False, task_id=None):
    clauses, params = [], []
    if query:
        clauses.append('(instr(lower(message),lower(?))>0 OR instr(lower(task_id),lower(?))>0)')
        params.extend([query,query])
    if task_id is not None:
        clauses.append('task_id=?'); params.append(task_id)
    if states is not None:
        states = tuple(states)
        if not states:
            return []
        clauses.append('display_state IN ('+','.join('?' for _ in states)+')')
        params.extend(str(state) for state in states)
    columns = '''task_id,conversation_id,substr(message,1,100) AS name,display_state,raw_state,
        created_at,updated_at,revision,publish_attempted,publish_state,
        COALESCE(json_extract(report,'$.completed_steps'),0) AS completed,
        COALESCE(NULLIF(json_extract(report,'$.total_steps'),0),json_array_length(plan,'$.steps'),0) AS total,
        substr(COALESCE(json_extract(report,'$.error'),json_extract(legacy_json,'$.error'),''),1,2000) AS error'''
    if not summary:
        columns += ',message,report,plan'
    sql = _BASE+'SELECT '+columns+' FROM normalized'+(' WHERE '+' AND '.join(clauses) if clauses else '')
    sql += ' ORDER BY created_at DESC,task_id DESC LIMIT ? OFFSET ?'
    params.extend([max(0,int(limit)),max(0,int(offset))])
    with store._db(write=False) as db:
        records = db.execute(sql,params).fetchall()
    rows = []
    for record in records:
        row = dict(record)
        row.update(id='agent:'+row['task_id'],kind='agent',tool='agent',state=row.pop('display_state'),command='')
        row['updated_at'] = f'{row["updated_at"]}:{row["revision"]}'
        if not summary:
            plan = json.loads(row.pop('plan'))
            row.update(items=plan.get('steps') or [],results=json.loads(row.pop('report')))
        rows.append(row)
    return rows
