"""Read-only availability of registered windows; never opens a browser."""
import hashlib
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

import httpx

from .process_locks import _try_lock, _unlock
from .session_store import SessionStore


def lock_busy(path):
    if not path.is_file():
        return False
    with path.open('r+b') as stream:
        if not _try_lock(stream):
            return True
        _unlock(stream)
    return False


def window_availability(registry_path, platform, *, client_factory=httpx.Client, temporary_root=None):
    root = Path(temporary_root or tempfile.gettempdir())
    records = [r for r in SessionStore(registry_path).list() if r.platform == platform]
    reachable, result = {}, []
    for record in records:
        try:
            parsed = urlsplit(record.api_url)
            port = parsed.port or 80
        except ValueError:
            continue
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1','localhost','::1'}
                or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment):
            continue
        if record.api_url not in reachable:
            try:
                with client_factory(timeout=2, trust_env=False) as client:
                    response = client.post(record.api_url.rstrip('/')+'/health', json={})
                    response.raise_for_status()
                    payload = response.json()
                    reachable[record.api_url] = isinstance(payload,dict) and payload.get('success') is True
            except (httpx.HTTPError, ValueError):
                reachable[record.api_url] = False
        status = '未找到'
        if reachable[record.api_url]:
            workflow_key = hashlib.sha256(f'loopback:{port}|{record.profile_id}'.encode()).hexdigest()
            operation_key = hashlib.sha256(f'{record.api_url}|{record.profile_id}'.encode()).hexdigest()
            busy = lock_busy(root/'social-agent-workflow-leases'/f'{workflow_key}.lock') or lock_busy(root/'social-agent-profile-locks'/f'{operation_key}.lock')
            status = '占用中' if busy else '可用（运行时校验）'
        result.append({'session_ref':record.session_ref, 'name':record.profile_name or record.profile_id,
                       'platform':record.platform, 'status':status, 'available':status=='可用（运行时校验）'})
    return result
