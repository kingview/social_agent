"""Quality inspection followed by receipt-backed material admission."""
from pathlib import Path


def import_material(context, item):
    _, original, metadata = context.source(item)
    replay = context.library.admission_receipt(context.job_id, original)
    if replay is not None:
        return replay
    staged = context.stage(original)
    with context.model_slot():
        inspection = context.call('inspect_material', {'file_path': str(staged)})
    context.check_control()
    candidate = Path(inspection.get('candidate_path', staged)).resolve(strict=True)
    if not candidate.is_relative_to(context.service.output_root):
        raise ValueError('检查插件返回了越界文件')
    inspection['source_path'] = str(original)
    if candidate == staged and original.is_relative_to(context.library.root):
        inspection['candidate_path'] = str(original)
    return context.library.admit(inspection, metadata=metadata, task_id=context.job_id)
