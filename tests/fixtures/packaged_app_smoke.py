"""Launch a packaged GUI with isolated data and no real credentials or plugins.

This checks startup only, not real platform or model behavior. The supervisor
terminates only the child it created, never another SocialAgent process.
"""
import argparse
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('app', type=Path)
    args = parser.parse_args()
    executable = args.app.resolve() / 'Contents/MacOS/SocialAgent'
    with TemporaryDirectory(prefix='social-agent-package-smoke-') as directory:
        root = Path(directory)
        environment = dict(os.environ)
        environment.update({
            'QT_QPA_PLATFORM': 'offscreen',
            'POSTDROP_SESSION_REGISTRY': str(root/'sessions.json'),
            'SOCIAL_AGENT_PLUGIN_ROOT': str(root/'plugins'),
            'SOCIAL_AGENT_LLM_SETTINGS_PATH': str(root/'llm.json'),
            'SOCIAL_AGENT_LLM_BASE_URL': 'http://127.0.0.1:9/v1',
            'SOCIAL_AGENT_LLM_MODEL': 'startup-test-only',
            'SOCIAL_AGENT_LLM_API_KEY': 'not-a-real-key',
            'SOCIAL_AGENT_LOG_DIR': str(root/'logs'),
        })
        process = subprocess.Popen([str(executable), '--output-root', str(root/'output')],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            try:
                output, _ = process.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                state = root/'output/.social-agent-state'
                for filename, table in (('tasks.sqlite3', 'tasks'), ('material-tasks.sqlite3', 'jobs')):
                    path = state/filename
                    assert path.is_file(), f'GUI failed to initialize {filename}'
                    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
                        assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
                assert any((state/'conversations').glob('*.json')), 'GUI did not create an isolated conversation'
                print('Packaged GUI initialized task stores and an isolated conversation; no task executed.')
            else:
                raise RuntimeError(f'Packaged GUI exited early ({process.returncode}): {output.decode(errors="replace")}')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    output, _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate(timeout=5)
                if b'Traceback (most recent call last)' in output:
                    raise RuntimeError(output.decode(errors='replace'))


if __name__ == '__main__':
    main()
