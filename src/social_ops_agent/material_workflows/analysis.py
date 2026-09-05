"""Local-model analysis with owner-scoped completion replay and safe recovery."""
import json
import mimetypes

from ..diagnostics import record_exception
from ..material_library import digest_file, strategy_scores
from ..material_task_state import MaterialJobInterrupted
from ..state_io import write_json


def analyze(context, item):
    resource, original, metadata = context.source(item)
    report_path = (
        context.service.output_root / 'material-analysis'
        / context.job_id / (digest_file(original) + '.json')
    )
    if resource is None:
        replay = local_report_receipt(report_path, original)
        if replay is not None:
            return replay
    lease_id = None
    if resource:
        claim = context.library.begin_analysis(resource['id'], job_id=context.job_id)
        if claim['replayed']:
            report = make_report(original, resource, claim['result'])
            write_json(report_path, report)
            return {**report, 'report_path': str(report_path)}
        lease_id = claim['lease_id']
    try:
        staged = context.stage(original)
        with context.model_slot():
            analysis = context.call('analyze_content', {
                'file_paths': [str(staged)],
                'analysis_profile': json.dumps({
                    'dimensions': context.settings.analysis_dimensions,
                    'tag_rules': context.settings.tag_rules,
                }, ensure_ascii=False),
                'post_text': metadata.get('text') or metadata.get('description'),
                'source_url': metadata.get('source_url'),
            })
        features = normalize_analysis(analysis, original)
        scores = strategy_scores(features, context.settings.strategies)
        if resource:
            state = context.library.save_analysis(
                resource['id'], analysis, features, scores,
                job_id=context.job_id, lease_id=lease_id,
            )
        else:
            state = (
                '需复核' if analysis.get('needs_human_review')
                or analysis.get('confidence', 0) < .7 else '已分析'
            )
        report = make_report(original, resource, {
            'analysis': analysis, 'features': features,
            'scores': scores, 'analysis_state': state,
        })
        write_json(report_path, report)
        return {**report, 'report_path': str(report_path)}
    except MaterialJobInterrupted:
        if resource:
            context.library.recover_analysis_job(context.job_id)
        raise
    except Exception as exc:
        if resource:
            # A committed successful analysis is a receipt, not a failure if only
            # exporting its report failed. Retry will replay without another call.
            if context.library.analysis_receipt(resource['id'], context.job_id) is None:
                try:
                    context.library.save_analysis(
                        resource['id'], {'error': str(exc)}, {}, [], failed=True,
                        job_id=context.job_id, lease_id=lease_id,
                    )
                except Exception as recovery_error:
                    record_exception(
                        'agent', 'materials.analysis.recovery', recovery_error,
                        state_root=context.service.state_root, task_id=context.job_id,
                    )
        raise


def normalize_analysis(analysis, original):
    if any(
        'Semantic model failed' in warning or 'Semantic model is not configured' in warning
        for warning in analysis.get('warnings', [])
    ):
        raise ValueError('本地语义模型执行失败；详见分析插件日志。未将回退结果当作已完成分析。')
    features = analysis.get('material_features') or {}
    features.setdefault('topic', analysis.get('topics', []))
    features.setdefault('language', [analysis.get('language', 'unknown')])
    features.setdefault('format', [mimetypes.guess_type(original.name)[0] or 'unknown'])
    if 'quality' not in features:
        analysis['needs_human_review'] = True
        analysis.setdefault('warnings', []).append('本地模型未返回基础质量评分，需复核')
    return features


def make_report(original, resource, result):
    report = {
        'analysis': result['analysis'], 'features': result['features'],
        'strategy_scores': result['scores'], 'analysis_state': result['analysis_state'],
        'source_path': str(original),
    }
    if resource:
        report['resource_id'] = resource['id']
    return report


def local_report_receipt(path, original):
    """Only replay this immutable job's completed report for the same file hash."""
    if not path.is_file():
        return None
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(report, dict)
        or report.get('source_path') != str(original)
        or report.get('analysis_state') not in {'已分析', '需复核'}
        or not isinstance(report.get('analysis'), dict)
        or not isinstance(report.get('features'), dict)
        or not isinstance(report.get('strategy_scores'), list)
    ):
        return None
    return {**report, 'report_path': str(path)}
