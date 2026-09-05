"""Read-only availability of registered windows; never opens a browser."""
from pathlib import Path
import tempfile

import httpx

from .browser_lock_contract import api_port
from .browser_occupancy import lock_busy, window_busy
from .session_store import SessionStore


def window_availability(registry_path, platform, *, client_factory=httpx.Client, temporary_root=None):
    root = Path(temporary_root or tempfile.gettempdir())
    records = [r for r in SessionStore(registry_path).list() if r.platform == platform]
    reachable, result = {}, []
    for record in records:
        try:
            api_port(record.api_url)
        except ValueError:
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
            busy = window_busy(root,record.api_url,record.profile_id,probe=lock_busy)
            status = '占用中' if busy else '可用（运行时校验）'
        result.append({'session_ref':record.session_ref, 'name':record.profile_name or record.profile_id,
                       'platform':record.platform, 'status':status, 'available':status=='可用（运行时校验）'})
    return result
