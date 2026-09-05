"""Read-only occupancy facade over the two existing browser lock namespaces."""
from .browser_lock_contract import lock_paths
from .process_locks import _try_lock, _unlock


def lock_busy(path):
    try:
        with path.open('r+b') as stream:
            if not _try_lock(stream):
                return True
            _unlock(stream)
        return False
    except FileNotFoundError:
        return False
    except OSError:
        # An unreadable lock is unknown, never proof that a window is idle.
        return True


def window_busy(temporary_root,api_url,profile_id,*,probe=None):
    probe = probe or lock_busy
    return any(probe(path) for path in lock_paths(temporary_root,api_url,profile_id))
