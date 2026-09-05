"""Material URL parsing and bounded sidecar metadata reads."""
import csv
import json
import re
from urllib.parse import urlsplit, urlunsplit


def parse_links(text):
    """Accept pasted URLs or CSV cells; never silently download whole channels."""
    urls, seen, rejected = [], set(), []
    for raw in re.findall(r'https?://[^\s<>"，,]+', text):
        raw = raw.rstrip('。；;）)')
        parsed = urlsplit(raw)
        host = (parsed.hostname or '').lower()
        domains = {'douyin.com','xiaohongshu.com','xhslink.com','t.me','telegram.me','x.com','twitter.com'}
        if parsed.scheme != 'https' or parsed.username or parsed.password or not any(host == d or host.endswith('.'+d) for d in domains):
            rejected.append(raw)
            continue
        if host in {'t.me','telegram.me'} and not re.fullmatch(r'/(?:s/)?[A-Za-z][A-Za-z0-9_]{3,63}/[1-9][0-9]*/?', parsed.path):
            rejected.append(raw)
            continue
        # Keep signed platform query strings for fetching, deduplicate their canonical path.
        identity = urlunsplit(('https', host, parsed.path.rstrip('/'), '', ''))
        if identity not in seen:
            seen.add(identity)
            urls.append(raw)
    return urls, rejected


def sidecar_metadata(path):
    result = {}
    for candidate in (path.with_suffix('.info.json'), path.with_suffix('.json'), path.parent / 'metadata.json'):
        if candidate.is_file() and candidate.stat().st_size <= 2*1024*1024:
            try:
                data = json.loads(candidate.read_text(encoding='utf-8'))
                if isinstance(data, list):
                    data = next((row for row in data if isinstance(row, dict) and row.get('path') in {str(path), path.name}), {})
                if isinstance(data, dict):
                    result.update({k:v for k,v in data.items() if k in {'title','text','description','author_name','uploader','published_at','upload_date','source_url','url','webpage_url','platform','metrics'}})
            except (OSError, ValueError):
                pass
    source_url = result.get('source_url') or result.get('webpage_url') or result.get('url')
    if source_url:
        result['source_url'] = source_url
        host = urlsplit(source_url).hostname or ''
        result.setdefault('platform', 'telegram' if host in {'t.me','telegram.me'} else 'xiaohongshu' if 'xiaohongshu' in host else 'douyin' if 'douyin' in host else 'x' if host in {'x.com','twitter.com'} else 'local')
    for candidate in (path.parent / 'text.txt', path.with_suffix('.txt')):
        if candidate.is_file() and candidate.stat().st_size <= 1024*1024:
            result.setdefault('text', candidate.read_text(encoding='utf-8', errors='replace'))
    csv_path = path.parent / 'metadata.csv'
    if csv_path.is_file() and csv_path.stat().st_size <= 2*1024*1024:
        with csv_path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                if row.get('path') in {path.name,str(path)} or row.get('filename') == path.name:
                    result.update({k:v for k,v in row.items() if k in {'title','text','author_name','source_url','published_at','platform'}})
                    break
    return result
