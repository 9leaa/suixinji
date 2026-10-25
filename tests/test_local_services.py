"""Local profiles must never inherit the other environment's credentials/data."""
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('local_services', Path(__file__).parents[1] / 'scripts/local_services.py')
services = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(services)


def test_profiles_isolate_database_credentials_and_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(services, 'PROFILES', tmp_path)
    for name, value in [('production', 'prod-db'), ('test', 'test-db')]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / '.env').write_text(f'DATABASE_URL={value}\n' + ('FEISHU_APP_SECRET=prod-secret\n' if name == 'production' else ''))
    monkeypatch.setenv('DATABASE_URL', 'inherited-db')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'inherited-secret')
    directory, env = services.environment('test')
    assert env['DATABASE_URL'] == 'test-db'
    assert 'FEISHU_APP_SECRET' not in env
    assert env['SUIXINJI_RUNTIME_DIR'] == str(directory) == str(tmp_path / 'test')
    assert env['SUIXINJI_ENV_FILE'] == str(directory / '.env')
    assert env['PYTHON_DOTENV_DISABLED'] == '1'


def test_test_profile_never_starts_feishu_receiver():
    roles = services.roles('test', {})
    assert set(roles) == {'console', 'worker-adaptive', 'scheduler', 'outbox-relay'}
    assert 'fact-memory-test' in roles['worker-adaptive']


def test_production_bind_comes_from_profile():
    roles = services.roles('production', {'SUIXINJI_API_HOST': '127.0.0.1', 'SUIXINJI_API_PORT': '18000'})
    assert roles['api'][-4:] == ['--host', '127.0.0.1', '--port', '18000']
    assert 'receiver' in roles
