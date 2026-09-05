"""Download dispatch and verified, per-item artifact export."""
from pathlib import Path
import shutil
import uuid

from ..material_library import digest_file
from ..state_io import write_json
from .inputs import parse_links


def download(context, item):
    urls, rejected = parse_links(str(item))
    if len(urls) != 1 or rejected:
        raise ValueError('下载项目必须是一个有效的具体帖子 URL')
    session = context.options.get('session_ref')
    limit = context.options.get('max_total_size_mb', 1000)
    if session:
        result = context.call('download_media', {
            'urls': urls, 'session_ref': session, 'max_total_size_mb': limit,
        })
    else:
        result = context.call('download_public_material', {
            'url': urls[0], 'max_total_size_mb': limit,
        })
    artifacts = result.get('artifacts', [])
    if not artifacts:
        return {**result, 'completed': False, 'error': '没有可下载媒体'}
    return export_artifacts(context, urls[0], result)


def export_artifacts(context, url, result):
    # Plugin originals remain intact. The destination is stable on item retry.
    destination = (
        Path(context.settings.download_root)
        / context.job_id / uuid.uuid5(uuid.NAMESPACE_URL, url).hex
    )
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    for index, artifact in enumerate(result['artifacts']):
        context.check_control()
        source = Path(artifact['path']).resolve(strict=True)
        if not source.is_relative_to(context.service.output_root):
            raise ValueError('插件返回了输出目录以外的媒体')
        target = destination / f'{index + 1:03d}{source.suffix}'
        if target != source:
            temporary = target.with_suffix(target.suffix + '.part')
            shutil.copy2(source, temporary)
            if digest_file(temporary) != artifact['sha256']:
                raise ValueError('下载复制校验失败')
            temporary.replace(target)
        copied.append({**artifact, 'path': str(target)})
    metadata = dict((result.get('items') or [{}])[0])
    metadata['source_url'] = url
    write_json(destination / 'metadata.json', metadata)
    (destination / 'text.txt').write_text(
        metadata.get('description') or metadata.get('text') or metadata.get('title') or '',
        encoding='utf-8',
    )
    return {**result, 'artifacts': copied, 'output_directory': str(destination)}
