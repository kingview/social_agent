"""Small, non-secret execution evidence for resuming across Harness restarts.

Never persist arbitrary tool arguments/results or use these records as authority.
Legacy recovery matches call IDs in the task journal, not model-written summaries.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .tool_results import result_payload


def _post_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        domains = ('t.me', 'telegram.me', 'web.telegram.org', 'x.com', 'twitter.com',
                   'xiaohongshu.com', 'xhslink.com', 'douyin.com', 'iesdouyin.com')
        if parsed.scheme != 'https' or parsed.username or parsed.password or not any(
                host == domain or host.endswith('.' + domain) for domain in domains):
            return None
        # Preserve public post access parameters, never auth/session parameters.
        query = urlencode([(k, v) for k, v in parse_qsl(parsed.query)
                           if k in {'xsec_token', 'xsec_source'}])
        fragment = parsed.fragment if host == 'web.telegram.org' and re.fullmatch(
            r'@?[A-Za-z0-9_-]{1,150}', parsed.fragment) else ''
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, fragment))
    except ValueError:
        return None


def resume_input(tool, arguments):
    if not isinstance(arguments, dict):
        return {}
    if tool == 'download_media':
        urls = arguments.get('urls')
        if not isinstance(urls, list) or not 0 < len(urls) <= 20:
            return {}
        safe = [_post_url(url) for url in urls]
        if not all(safe):
            return {}  # Never silently drop a target and change the batch.
        result = {'urls': safe}
        for key in ('media_format', 'max_total_size_mb', 'telegram_scope', 'telegram_max_messages'):
            value = arguments.get(key)
            if type(value) in (str, int) and len(str(value)) < 100:
                result[key] = value
        return result
    return {}


def resume_output(tool, payload):
    if not isinstance(payload, dict):
        return {}
    if tool == 'browse_posts':
        posts = payload.get('posts')
        if not isinstance(posts, list):
            return {}
        urls = [_post_url(post.get('url')) for post in posts[:100] if isinstance(post, dict)]
        return {'post_urls': [url for url in urls if url]}
    if tool == 'download_media':
        result = {}
        for key in ('output_dir', 'output_directory', 'checkpoint_path'):
            value = payload.get(key)
            if isinstance(value, str) and len(value) <= 4096 and Path(value).is_absolute():
                result[key] = value
        artifacts = payload.get('artifacts', [])
        if isinstance(artifacts, list):
            result['artifact_paths'] = [a['path'] for a in artifacts[:200] if isinstance(a, dict)
                and isinstance(a.get('path'), str) and len(a['path']) <= 4096 and Path(a['path']).is_absolute()]
        return result
    return {}


def recover_legacy_calls(root: Path, conversation_id: str, calls: dict) -> dict:
    """Read only matching historical calls; never run them or grant new access."""
    recovered = {key: dict(value) for key, value in calls.items() if isinstance(value, dict)}
    wanted = {key for key, value in recovered.items()
              if value.get('tool') in {'browse_posts', 'download_media'}
              and not (value.get('resume_input') or value.get('resume_output'))}
    if not wanted:
        return recovered
    for path in root.glob('*/*/session.jsonl'):
        if not path.parent.name.startswith(conversation_id + '-execute-') or path.is_symlink():
            continue
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                continue
            with path.open() as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                        data = event.get('data')
                        if not isinstance(data, dict):
                            continue
                        if event.get('type') == 'tool/call' and data.get('callId') in wanted:
                            row = recovered[data['callId']]
                            if data.get('name') != 'mcp__social__' + row['tool']:
                                continue
                            args = data.get('arguments')
                            args = json.loads(args) if isinstance(args, str) else args
                            row['resume_input'] = resume_input(row['tool'], args)
                        elif event.get('type') == 'tool/result':
                            for block in data.get('message', {}).get('content', []):
                                if block.get('type') != 'tool-result' or block.get('toolCallId') not in wanted or block.get('isError'):
                                    continue
                                row = recovered[block['toolCallId']]
                                row['resume_output'] = resume_output(row['tool'], result_payload(block.get('content')))
                    except (ValueError, TypeError, AttributeError):
                        continue
        except OSError:
            continue
    return recovered


def evidence_summary(calls: dict) -> list[dict]:
    return [{key: row[key] for key in ('tool', 'status', 'resume_input', 'resume_output') if key in row}
            for row in calls.values() if row.get('resume_input') or row.get('resume_output')][-40:]
