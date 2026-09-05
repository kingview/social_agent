"""Local material records. Failed inspection attempts never become assets."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


@contextmanager
def rollback_generated_copies():
    """Only remove files created by this admission if the DB commit fails."""
    paths = []
    try:
        yield paths
    except BaseException:
        _remove_generated_copies(paths)
        raise


def _remove_generated_copies(paths):
    """Paths belong to a newly and exclusively created admission directory."""
    for path in reversed(paths):
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def strategy_scores(features, rules):
    """Deterministic, explainable weighting over model-provided content features."""
    scores = []
    for rule in rules:
        if not rule.enabled:
            continue
        def values(key):
            raw = features.get(key, [])
            return {str(v).casefold() for v in (raw if isinstance(raw, list) else [raw])}
        excluded = [key for key, allowed in rule.required.items()
                    if allowed and not values(key).intersection(str(x).casefold() for x in allowed)]
        dimensions = {}
        missing = []
        hits = []
        for key, weight in rule.weights.items():
            if weight <= 0:
                continue
            if key == 'quality':
                raw = features.get('quality')
                if isinstance(raw, (int, float)) and 0 <= raw <= 100:
                    dimensions[key] = float(raw)
                else:
                    dimensions[key] = 0
                    missing.append(key)
            elif key in rule.preferred and rule.preferred[key]:
                matched = values(key).intersection(str(x).casefold() for x in rule.preferred[key])
                dimensions[key] = 100 if matched else 0
                if matched:
                    hits.append(key)
                elif key not in features:
                    missing.append(key)
            else:
                dimensions[key] = 0
                missing.append(key)
        score = round(sum(dimensions[k] * rule.weights[k] for k in dimensions) / sum(rule.weights.values()), 2)
        scores.append({'strategy': rule.name, 'score': 0 if excluded else score,
                       'matched_dimensions': hits, 'excluded_reasons': excluded,
                       'missing_dimensions': missing, 'dimension_scores': dimensions,
                       'recommendation': '排除' if excluded else '待复核' if missing else
                           '建议使用' if score >= rule.minimum_score else '不建议'})
    return scores or [{'status': '待配置'}]


class MaterialLibrary:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'resources.db'
        if self.path.is_file() and self._schema_current():
            return
        with self.db() as db:
            receipts_existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admission_receipts'").fetchone() is not None
            for statement in '''
                CREATE TABLE IF NOT EXISTS resources (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL, source_path TEXT NOT NULL,
                    sha256 TEXT UNIQUE NOT NULL, source_sha256 TEXT NOT NULL,
                    media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, phash TEXT,
                    created_at TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    acquisition_state TEXT NOT NULL, intake_state TEXT NOT NULL,
                    analysis_state TEXT NOT NULL, usage_state TEXT NOT NULL,
                    analysis_json TEXT, features_json TEXT, scores_json TEXT,
                    manual_subject_group TEXT);
                CREATE TABLE IF NOT EXISTS intake_attempts (
                    id TEXT PRIMARY KEY, task_id TEXT, source_path TEXT NOT NULL,
                    state TEXT NOT NULL, issues_json TEXT NOT NULL, resource_id TEXT,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS resource_events (
                    id INTEGER PRIMARY KEY, resource_id TEXT, action TEXT,
                    detail_json TEXT, time TEXT);
                CREATE TABLE IF NOT EXISTS admission_receipts (
                    task_id TEXT NOT NULL, source_path TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL, sha256 TEXT NOT NULL,
                    resource_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, source_path, source_sha256, sha256));
            '''.split(';'):
                if statement.strip():
                    db.execute(statement)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(resources)')}
            for column in ('analysis_job_id', 'analysis_lease_id', 'analysis_updated_at'):
                if column not in columns:
                    db.execute(f'ALTER TABLE resources ADD COLUMN {column} TEXT')
            db.execute('CREATE INDEX IF NOT EXISTS resources_analysis_owner ON resources(analysis_job_id, analysis_state)')
            db.execute('CREATE INDEX IF NOT EXISTS intake_attempts_task ON intake_attempts(task_id, source_path)')
            if not receipts_existed:
                db.execute('''INSERT OR IGNORE INTO admission_receipts
                    SELECT a.task_id,a.source_path,r.source_sha256,r.sha256,r.id,a.created_at
                    FROM intake_attempts a JOIN resources r ON r.id=a.resource_id
                    WHERE a.task_id IS NOT NULL AND a.task_id!='' AND a.state='已入库' ''')

    def _schema_current(self):
        # Services can construct this repository while another task is writing.
        # Once migrated, opening it must remain a read-only operation.
        with self.read_db() as db:
            tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'resources', 'intake_attempts', 'resource_events', 'admission_receipts'} <= tables:
                return False
            columns = {row['name'] for row in db.execute('PRAGMA table_info(resources)')}
            return {'analysis_job_id', 'analysis_lease_id', 'analysis_updated_at'} <= columns

    @contextmanager
    def db(self):
        """A short writer transaction; keep hashing, copies and model work outside."""
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def read_db(self):
        """Read connections do not reserve SQLite's sole writer lock."""
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            yield db
        finally:
            db.close()

    @staticmethod
    def _record_attempt(db, source, state, issues, *, task_id=None, resource_id=None):
        attempt_id = uuid.uuid4().hex
        db.execute('INSERT INTO intake_attempts VALUES(?,?,?,?,?,?,?)',
                   (attempt_id, task_id, str(source), state, json.dumps(issues, ensure_ascii=False), resource_id,
                    datetime.now(timezone.utc).isoformat()))
        return attempt_id

    def record_attempt(self, source, state, issues, *, task_id=None, resource_id=None):
        with self.db() as db:
            return self._record_attempt(db, source, state, issues, task_id=task_id, resource_id=resource_id)

    @staticmethod
    def _admission_result(resource):
        return {'resource_id': resource['id'], 'path': resource['path'],
                'source_path': resource['source_path'], 'intake_state': '已入库', 'issues': []}

    @staticmethod
    def _receipt(db, task_id, source, source_digest, digest=None):
        if not task_id:
            return None
        sql = '''SELECT r.* FROM admission_receipts a JOIN resources r ON r.id=a.resource_id
                 WHERE a.task_id=? AND a.source_path=? AND a.source_sha256=?'''
        parameters = [task_id, str(source), source_digest]
        if digest:
            sql += ' AND a.sha256=?'
            parameters.append(digest)
        return db.execute(sql + ' ORDER BY a.created_at DESC LIMIT 1', parameters).fetchone()

    def admission_receipt(self, task_id, source_path):
        """Replay an acknowledged admission only while both original and copy match."""
        if not task_id:
            return None
        source = Path(source_path).resolve(strict=True)
        source_digest = digest_file(source)
        with self.read_db() as db:
            receipt = self._receipt(db, task_id, source, source_digest)
        if receipt is None:
            return None
        target = Path(receipt['path'])
        if not target.is_file() or digest_file(target) != receipt['sha256']:
            raise ValueError('已入库文件丢失或发生变化，不能复用本次入库结果')
        return self._admission_result(receipt)

    @staticmethod
    def _duplicate(db, digest, phash):
        duplicate = db.execute('SELECT id FROM resources WHERE sha256=?', (digest,)).fetchone()
        if duplicate is None and phash:
            for row in db.execute('SELECT id,phash FROM resources WHERE phash IS NOT NULL AND usage_state != ?', ('已删除',)):
                if len(phash) == len(row['phash']) and (int(phash, 16) ^ int(row['phash'], 16)).bit_count() <= 4:
                    return row
        return duplicate

    def admit(self, inspection: dict, *, metadata=None, task_id=None):
        source = Path(inspection['source_path']).resolve(strict=True)
        if not inspection.get('passed'):
            self.record_attempt(source, '未通过', inspection.get('issues', []), task_id=task_id)
            return {'source_path': str(source), 'intake_state': '未通过', 'issues': inspection.get('issues', [])}
        candidate = Path(inspection['candidate_path']).resolve(strict=True)
        digest = digest_file(candidate)
        if digest != inspection.get('sha256'):
            raise ValueError('检查后文件发生变化，必须重新检查')
        source_digest = digest if source == candidate else digest_file(source)
        info = dict(metadata or {})
        platform = info.get('platform', 'local')
        platform = platform if platform in {'local', 'telegram', 'douyin', 'xiaohongshu', 'x'} else 'local'
        now = datetime.now(timezone.utc)
        resource_id = uuid.uuid4().hex
        phash = inspection.get('phash')
        with self.read_db() as db:
            receipt = self._receipt(db, task_id, source, source_digest, digest)
            duplicate = self._duplicate(db, digest, phash)
        if receipt is not None:
            return self.admission_receipt(task_id, source)
        if duplicate is not None:
            with self.db() as db:
                receipt = self._receipt(db, task_id, source, source_digest, digest)
                duplicate = self._duplicate(db, digest, phash)
                if receipt is None and duplicate is not None:
                    self._record_attempt(db, source, '未通过', ['重复'], task_id=task_id)
            if receipt is not None:
                return self.admission_receipt(task_id, source)
            if duplicate is not None:
                return {'source_path': str(source), 'intake_state': '未通过',
                        'issues': ['重复'], 'duplicate_of': duplicate['id']}
        with rollback_generated_copies() as created:
            target = candidate
            if not candidate.is_relative_to(self.root):
                folder = self.root / platform / now.strftime('%Y/%m') / resource_id
                folder.mkdir(parents=True, exist_ok=False)
                target = folder / ('01' + candidate.suffix.lower())
                temporary = target.with_suffix(target.suffix + '.part')
                created.append(temporary)
                shutil.copy2(candidate, temporary)
                if digest_file(temporary) != digest:
                    raise OSError('复制校验失败，未入库')
                temporary.replace(target)
                created.append(target)
            size_bytes = target.stat().st_size
            # Another process can admit while this copy is in progress. Recheck
            # both the task receipt and content identity under the writer lock.
            with self.db() as db:
                receipt = self._receipt(db, task_id, source, source_digest, digest)
                duplicate = self._duplicate(db, digest, phash)
                if receipt is not None:
                    result = self._admission_result(receipt)
                elif duplicate:
                    result = {'source_path': str(source), 'intake_state': '未通过',
                              'issues': ['重复'], 'duplicate_of': duplicate['id']}
                    self._record_attempt(db, source, '未通过', ['重复'], task_id=task_id)
                else:
                    db.execute('''INSERT INTO resources
                        (id,path,source_path,sha256,source_sha256,media_type,size_bytes,phash,
                         created_at,metadata_json,acquisition_state,intake_state,analysis_state,
                         usage_state,analysis_json,features_json,scores_json,manual_subject_group)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (resource_id, str(target), str(source), digest, source_digest,
                         inspection['media_type'], size_bytes, phash, now.isoformat(),
                         json.dumps(info, ensure_ascii=False), '下载成功' if info.get('source_url') else '不适用',
                         '已入库', '未分析', '未使用', None, None, None, None))
                    self._record_attempt(db, source, '已入库', [], task_id=task_id, resource_id=resource_id)
                    if task_id:
                        db.execute('INSERT INTO admission_receipts VALUES(?,?,?,?,?,?)',
                                   (task_id, str(source), source_digest, digest, resource_id, now.isoformat()))
                    result = {'resource_id': resource_id, 'path': str(target), 'source_path': str(source),
                              'intake_state': '已入库', 'issues': []}
            if result.get('resource_id') != resource_id:
                _remove_generated_copies(created)
        return result

    @staticmethod
    def _pagination(sql, params, limit, offset):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError('分页偏移必须是非负整数')
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError('分页数量必须是非负整数')
        if limit is not None or offset:
            sql += ' LIMIT ? OFFSET ?'
            params.extend([-1 if limit is None else limit, offset])
        return sql, params

    def list(self, *, query='', analysis_state=None, usage_state=None, media_type=None, theme=None,
             subject_group=None, minimum_quality=None, strategy=None, limit=None, offset=0):
        sql, params = ('SELECT * FROM resources WHERE (source_path LIKE ? OR metadata_json LIKE ? OR id LIKE ? '
                       'OR analysis_json LIKE ? OR features_json LIKE ? OR manual_subject_group LIKE ?)'), ['%'+query+'%']*6
        for field, value in [('analysis_state', analysis_state), ('usage_state', usage_state)]:
            if value:
                sql += f' AND {field}=?'
                params.append(value)
        if media_type:
            if media_type not in {'image','video'}: raise ValueError('媒体类型必须为图片或视频')
            sql += ' AND media_type LIKE ?'; params.append(media_type+'/%')
        if theme:
            sql += " AND json_extract(metadata_json,'$.theme')=?"; params.append(theme)
        if subject_group:
            sql += ' AND manual_subject_group=?'; params.append(subject_group)
        if minimum_quality is not None:
            if isinstance(minimum_quality,bool) or not isinstance(minimum_quality,(int,float)) or not 0<=minimum_quality<=100:
                raise ValueError('基础质量分必须在 0–100 之间')
            sql += " AND json_type(features_json,'$.quality') IN ('integer','real') AND json_extract(features_json,'$.quality')>=?"
            params.append(minimum_quality)
        if strategy:
            sql += " AND EXISTS (SELECT 1 FROM json_each(scores_json) s WHERE json_extract(s.value,'$.strategy')=? AND json_extract(s.value,'$.recommendation')='建议使用')"
            params.append(strategy)
        sql, params = self._pagination(sql + ' ORDER BY created_at DESC,id DESC', params, limit, offset)
        with self.read_db() as db:
            return [dict(r) for r in db.execute(sql, params)]

    def attempts(self, *, failed_only=True, limit=None, offset=0):
        sql = 'SELECT * FROM intake_attempts' + (" WHERE state='未通过'" if failed_only else '')
        sql, params = self._pagination(sql + ' ORDER BY created_at DESC,id DESC', [], limit, offset)
        with self.read_db() as db:
            return [dict(row) for row in db.execute(sql, params)]

    def get(self, resource_id):
        with self.read_db() as db:
            row = db.execute('SELECT * FROM resources WHERE id=?', (resource_id,)).fetchone()
        if row is None:
            raise ValueError('素材不存在')
        return dict(row)

    @staticmethod
    def _analysis_result(resource):
        if resource['analysis_state'] not in ('已分析', '需复核') or not resource['analysis_json']:
            return None
        return {'analysis_state': resource['analysis_state'],
                'analysis': json.loads(resource['analysis_json']),
                'features': json.loads(resource['features_json'] or '{}'),
                'scores': json.loads(resource['scores_json'] or '[]')}

    def get_analysis_result(self, resource_id, *, job_id=None):
        resource = self.get(resource_id)
        if job_id is not None and resource['analysis_job_id'] != job_id:
            return None
        return self._analysis_result(resource)

    def analysis_receipt(self, resource_id, job_id):
        return self.get_analysis_result(resource_id, job_id=job_id)

    def begin_analysis(self, resource_id, *, job_id=None):
        with self.db() as db:
            resource = db.execute('SELECT * FROM resources WHERE id=?', (resource_id,)).fetchone()
            if resource is None:
                raise ValueError('素材不存在')
            if resource['intake_state'] != '已入库' or resource['usage_state'] in ('停用', '已删除'):
                raise ValueError('素材当前状态不可分析')
            if job_id and resource['analysis_job_id'] == job_id:
                result = self._analysis_result(resource)
                if result is not None:
                    return {'replayed': True, 'lease_id': None, 'result': result}
            lease_id = uuid.uuid4().hex
            changed = db.execute("""UPDATE resources SET analysis_state='分析中', analysis_job_id=?,
                analysis_lease_id=?, analysis_updated_at=? WHERE id=? AND intake_state='已入库'
                AND usage_state NOT IN ('停用','已删除') AND analysis_state IN ('未分析','分析失败','需复核')""",
                (job_id, lease_id, datetime.now(timezone.utc).isoformat(), resource_id))
            if changed.rowcount != 1:
                raise ValueError('素材当前状态不可分析，或已有分析任务')
        return {'replayed': False, 'lease_id': lease_id, 'result': None}

    def save_analysis(self, resource_id, analysis, features, scores, *, failed=False, job_id=None, lease_id=None):
        state = '分析失败' if failed else '需复核' if analysis.get('needs_human_review') or analysis.get('confidence', 0) < .7 else '已分析'
        with self.db() as db:
            resource = db.execute('SELECT * FROM resources WHERE id=?', (resource_id,)).fetchone()
            if resource is None:
                raise ValueError('素材不存在')
            if resource['analysis_job_id'] != job_id:
                raise ValueError('分析任务已失去素材所有权，忽略过期结果')
            if job_id is not None and resource['analysis_state'] != '分析中':
                raise ValueError('分析任务不再持有执行租约，忽略过期结果')
            if lease_id is not None and resource['analysis_lease_id'] != lease_id:
                raise ValueError('分析任务租约已失效，忽略过期结果')
            db.execute('''UPDATE resources SET analysis_state=?,analysis_json=?,features_json=?,scores_json=?,
                       analysis_lease_id=NULL,analysis_updated_at=? WHERE id=?''',
                (state, json.dumps(analysis, ensure_ascii=False), json.dumps(features, ensure_ascii=False),
                 json.dumps(scores, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), resource_id))
        return state

    def recover_analysis_job(self, job_id):
        """Call only after the job ledger has proved this worker interrupted."""
        if not job_id:
            return 0
        with self.db() as db:
            changed = db.execute("""UPDATE resources SET analysis_state='分析失败',
                analysis_job_id=NULL,analysis_lease_id=NULL,analysis_updated_at=?
                WHERE analysis_job_id=? AND analysis_state='分析中'""",
                (datetime.now(timezone.utc).isoformat(), job_id))
            return changed.rowcount

    def recover_legacy_analysis(self, resource_ids):
        """Recover explicit pre-owner rows only after their job lock is held.

        New-version callers without a job ID still receive a lease, so checking
        both nullable fields excludes their live or otherwise owned work.
        """
        resource_ids = tuple(dict.fromkeys(resource_ids))
        if not resource_ids:
            return 0
        placeholders = ','.join('?' for _ in resource_ids)
        with self.db() as db:
            changed = db.execute(
                "UPDATE resources SET analysis_state='分析失败',analysis_updated_at=? "
                "WHERE analysis_state='分析中' AND analysis_job_id IS NULL "
                "AND analysis_lease_id IS NULL AND id IN (" + placeholders + ')',
                (datetime.now(timezone.utc).isoformat(), *resource_ids),
            )
            return changed.rowcount

    def rescore(self, rules):
        """Compute outside a write transaction; do not overwrite newer analysis."""
        with self.read_db() as db:
            resources = db.execute("SELECT id,features_json FROM resources WHERE analysis_state IN ('已分析','需复核')").fetchall()
        updated = [(json.dumps(strategy_scores(json.loads(row['features_json'] or '{}'), rules), ensure_ascii=False),
                    row['id'], row['features_json']) for row in resources]
        changed = 0
        with self.db() as db:
            for scores, resource_id, original_features in updated:
                changed += db.execute("""UPDATE resources SET scores_json=? WHERE id=?
                    AND features_json IS ? AND analysis_state IN ('已分析','需复核')""",
                    (scores, resource_id, original_features)).rowcount
        return changed

    def review(self, resource_id, *, subject_group=None):
        with self.db() as db:
            changed = db.execute("UPDATE resources SET analysis_state='已分析',manual_subject_group=? WHERE id=? AND analysis_state='需复核'", (subject_group, resource_id))
            if changed.rowcount != 1:
                raise ValueError('只有需复核素材可以确认')
            db.execute('INSERT INTO resource_events(resource_id,action,detail_json,time) VALUES(?,?,?,?)',
                (resource_id, '人工复核', json.dumps({'subject_group': subject_group}), datetime.now(timezone.utc).isoformat()))

    def set_usage(self, resource_id, state):
        if state not in {'未使用', '已占用', '已使用', '停用', '已删除'}:
            raise ValueError('无效使用状态')
        with self.db() as db:
            db.execute('UPDATE resources SET usage_state=? WHERE id=?', (state, resource_id))
        # Soft deletion only: original and library files are preserved.
