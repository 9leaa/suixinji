#!/usr/bin/env python3
"""Create a console account or reset its password in an explicitly selected environment."""
import argparse
import getpass
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'reset-password'])
    parser.add_argument('username')
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--alias', default='管理员')
    parser.add_argument('--admin', action='store_true', help='Only applies when creating an account')
    args = parser.parse_args()
    from dotenv import dotenv_values
    config = args.env_file.resolve()
    if not config.is_file():
        parser.error('Environment file does not exist')
    os.environ.update({k: v for k, v in dotenv_values(config).items() if v is not None})
    os.environ['SUIXINJI_ENV_FILE'] = str(config)
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    password = getpass.getpass('Password (at least 12 characters): ')
    if password != getpass.getpass('Confirm password: '):
        parser.error('Passwords do not match')
    from apps.console_auth import Registration
    from repositories.postgres import console_auth as store
    body = Registration(username=args.username, password=password, alias=args.alias)
    account = store.find_account(body.username)
    if args.action == 'create':
        if account:
            parser.error('Username already exists')
        store.create_account(body.username, body.password, body.alias, is_admin=args.admin)
    else:
        if account is None:
            parser.error('Username does not exist')
        store.change_password(account['id'], body.password)
    print('Account updated:', body.username)


if __name__ == '__main__':
    main()
