"""Console accounts and revocable sessions; one private tenant per account."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, MetaData, String, Table, delete, select

from infrastructure.database import get_engine

metadata = MetaData()
accounts = Table('console_accounts', metadata,
    Column('id', String(64), primary_key=True),
    Column('username', String(64), nullable=False, unique=True),
    Column('password_hash', String(256), nullable=False),
    Column('tenant_id', String(128), nullable=False, unique=True),
    Column('user_id', String(255), nullable=False, unique=True),
    Column('space_id', String(255), nullable=False, unique=True),
    Column('is_admin', Boolean, nullable=False, default=False),
    Column('created_at', DateTime(timezone=True), nullable=False))
sessions = Table('console_sessions', metadata,
    Column('token_hash', String(64), primary_key=True),
    Column('account_id', ForeignKey('console_accounts.id', ondelete='CASCADE'), nullable=False, index=True),
    Column('expires_at', DateTime(timezone=True), nullable=False, index=True))
SESSION_SECONDS = 7 * 24 * 3600


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=1, maxmem=64*1024*1024)
    return 'scrypt$' + salt + '$' + digest.hex()


def verify_password(password, encoded):
    _, salt, expected = encoded.split('$')
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=1, maxmem=64*1024*1024)
    return hmac.compare_digest(digest.hex(), expected)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def provision_space(conn, account, alias):
    from infrastructure.schema import Tenant, User, Space, SpaceMember
    conn.execute(Tenant.__table__.insert().values(id=account['tenant_id'], name=account['username']))
    conn.execute(User.__table__.insert().values(id=account['user_id'], tenant_id=account['tenant_id'],
                 source='console', source_user_id=account['user_id'], profile_json={'alias': alias}))
    conn.execute(Space.__table__.insert().values(id=account['space_id'], tenant_id=account['tenant_id'],
                 source='console', source_space_id=account['space_id'], space_type='personal'))
    conn.execute(SpaceMember.__table__.insert().values(tenant_id=account['tenant_id'],
                 space_id=account['space_id'], user_id=account['user_id'], role='owner'))


def create_account(username, password, alias, *, is_admin=False):
    ident = uuid.uuid4().hex
    account = dict(id=ident, username=username, password_hash=hash_password(password),
                   tenant_id='account_' + ident, user_id='user_' + ident, space_id='personal_' + ident,
                   is_admin=is_admin, created_at=datetime.now(timezone.utc))
    with get_engine().begin() as conn:
        conn.execute(accounts.insert().values(**account))
        provision_space(conn, account, alias)
    return account


def find_account(username):
    with get_engine().connect() as conn:
        row = conn.execute(select(accounts).where(accounts.c.username == username)).mappings().first()
        return dict(row) if row else None


def create_session(account_id, previous=None):
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        conn.execute(delete(sessions).where(sessions.c.expires_at <= now))
        if previous:
            conn.execute(delete(sessions).where(sessions.c.token_hash == token_hash(previous)))
        conn.execute(sessions.insert().values(token_hash=token_hash(token), account_id=account_id,
                     expires_at=now + timedelta(seconds=SESSION_SECONDS)))
    return token


def resolve_session(token):
    if not token:
        return None
    with get_engine().connect() as conn:
        row = conn.execute(select(accounts).join(sessions, accounts.c.id == sessions.c.account_id).where(
            sessions.c.token_hash == token_hash(token), sessions.c.expires_at > datetime.now(timezone.utc))).mappings().first()
        return dict(row) if row else None


def revoke_session(token):
    with get_engine().begin() as conn:
        conn.execute(delete(sessions).where(sessions.c.token_hash == token_hash(token)))


def change_password(account_id, password):
    encoded = hash_password(password)
    with get_engine().begin() as conn:
        conn.execute(accounts.update().where(accounts.c.id == account_id).values(password_hash=encoded))
        conn.execute(delete(sessions).where(sessions.c.account_id == account_id))
