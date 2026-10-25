#!/usr/bin/env python3
"""Manage local production/test services sharing one source checkout."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import time

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / 'environments'


def environment(profile):
    directory = PROFILES / profile
    config = directory / '.env'
    if not config.is_file():
        raise SystemExit(f'Missing configuration: {config}')
    env = dict(os.environ)
    for name in ('production', 'test'):
        for key in dotenv_values(PROFILES / name / '.env'):
            env.pop(key, None)
    env.update({k: v for k, v in dotenv_values(config).items() if v is not None})
    env.update(PYTHONPATH=str(ROOT), PYTHON_DOTENV_DISABLED='1',
               SUIXINJI_RUNTIME_DIR=str(directory), SUIXINJI_ENV_FILE=str(config))
    return directory, env


def roles(profile, env):
    python = str(ROOT / '.venv/bin/python')
    if profile == 'test':
        return {
            'console': [python, str(ROOT / 'scripts/run_console.py')],
            'worker-adaptive': [python, '-m', 'apps.worker', 'adaptive', '--worker-id', 'fact-memory-test'],
            'scheduler': [python, '-m', 'apps.scheduler'],
            'outbox-relay': [python, '-m', 'apps.outbox_relay'],
        }
    result = {
        'outbox-relay': [python, '-m', 'apps.outbox_relay'],
        'scheduler': [python, '-m', 'apps.scheduler'],
        'api': [python, '-m', 'uvicorn', 'apps.api:app', '--host', env.get('SUIXINJI_API_HOST', '127.0.0.1'),
                '--port', env.get('SUIXINJI_API_PORT', '18000')],
        'receiver': [python, '-m', 'bot.feishu_bot'],
    }
    for role in ('ingest', 'query', 'summary', 'memory', 'memory_embedding', 'semantic_profile_projection', 'enrichment', 'delivery'):
        result['worker-' + role.replace('_', '-')] = [python, '-m', 'apps.worker', role]
    return result


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def manage(profile, action):
    directory, env = environment(profile)
    pid_dir, log_dir = directory / 'data/pids', directory / 'data/logs'
    pid_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    pending = []
    for role, command in roles(profile, env).items():
        path = pid_dir / (role + '.pid')
        pid = int(path.read_text()) if path.exists() else None
        running = pid is not None and alive(pid)
        if action == 'status':
            print(f'{profile}/{role}: {"running" if running else "stopped"}')
        elif action == 'stop':
            if running:
                os.kill(pid, signal.SIGTERM)
                pending.append((pid, path))
            else:
                path.unlink(missing_ok=True)
        elif not running:
            role_env = {**env, 'SUIXINJI_PROCESS_ROLE': 'api' if role == 'console' else role}
            if role == 'receiver' and env.get('FEISHU_PROXY_URL'):
                role_env.update(HTTP_PROXY=env['FEISHU_PROXY_URL'], HTTPS_PROXY=env['FEISHU_PROXY_URL'],
                                NO_PROXY='127.0.0.1,localhost,::1')
            with (log_dir / (role + '.log')).open('ab') as log:
                proc = subprocess.Popen(command, cwd=directory, env=role_env,
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            path.write_text(str(proc.pid) + '\n')
            print(f'{profile}/{role}: started {proc.pid}')
    deadline = time.monotonic() + 30
    while pending and time.monotonic() < deadline:
        for pid, path in pending[:]:
            if not alive(pid):
                path.unlink(missing_ok=True)
                pending.remove((pid, path))
        if pending:
            time.sleep(.2)
    if pending:
        raise SystemExit('Some services are still stopping; PID files retained. Retry status before restarting.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'status'))
    parser.add_argument('profile', choices=('production', 'test', 'all'), default='all', nargs='?')
    args = parser.parse_args()
    for name in (('production', 'test') if args.profile == 'all' else (args.profile,)):
        manage(name, args.action)
