"""Cookie authentication for the public console. Legacy tokens are local-only opt-in."""
import hashlib
import os

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError

from repositories.postgres import console_auth as store

router = APIRouter(prefix='/api/console/auth')
COOKIE = 'sxj_session'
# A fixed valid hash gives unknown usernames the same password verification work.
DUMMY_HASH = 'scrypt$' + '00' * 16 + '$' + '00' * 64


def accounts_enabled():
    mode = os.getenv('SUIXINJI_CONSOLE_AUTH_MODE', 'accounts')
    if mode not in {'accounts', 'token'}:
        raise RuntimeError('SUIXINJI_CONSOLE_AUTH_MODE must be accounts or token')
    return mode == 'accounts'


def enabled():
    if os.getenv('SUIXINJI_CONSOLE_ENABLED') != '1' or not accounts_enabled():
        raise HTTPException(404, 'not found')


def check_mutation(request):
    if request.method not in {'GET', 'HEAD', 'OPTIONS'} and request.headers.get('X-Suixinji-Request') != 'console':
        raise HTTPException(403, '请求来源无效，请刷新页面重试')


def authenticated(request):
    enabled()
    check_mutation(request)
    account = store.resolve_session(request.cookies.get(COOKIE))
    if account is None:
        raise HTTPException(401, '请先登录')
    request.state.account = account
    return account


def current_account(request):
    return getattr(request.state, 'account', None) if request else None


def require_admin(request):
    account = current_account(request)
    if account and not account['is_admin']:
        raise HTTPException(403, '此功能仅供管理员使用')


def public_account(account):
    return {key: account[key] for key in ('username', 'user_id', 'space_id', 'is_admin')}


def limit_auth(request, username):
    from infrastructure.redis_rate_limit import RedisRateLimiter
    from infrastructure.redis_keys import KEYS
    from redis.exceptions import RedisError
    ip = request.client.host if request.client else 'unknown'
    try:
        limiter = RedisRateLimiter()
        for kind, value, limit in [('ip', ip, 30), ('username', username, 10)]:
            key = KEYS.rate_user('console-auth', hashlib.sha256(value.encode()).hexdigest(), kind)
            result = limiter.allow(key, limit, window_seconds=60)
            if not result.allowed:
                raise HTTPException(429, '尝试次数过多，请稍后再试', headers={'Retry-After': '60'})
    except RedisError:
        raise HTTPException(503, '登录服务暂不可用，请稍后重试') from None


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r'^[a-zA-Z0-9_\-]+$')
    password: str = Field(min_length=12, max_length=128)

    @field_validator('username')
    @classmethod
    def normalize(cls, value):
        return value.lower()


class Registration(Credentials):
    alias: str = Field(min_length=1, max_length=40)

    @field_validator('alias')
    @classmethod
    def clean_alias(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError('请填写有效的称呼')
        return value


def start_session(request, response, account):
    token = store.create_session(account['id'], request.cookies.get(COOKIE))
    response.set_cookie(COOKIE, token, max_age=store.SESSION_SECONDS, httponly=True, samesite='strict',
                        secure=os.getenv('SUIXINJI_CONSOLE_COOKIE_SECURE', 'true').lower() == 'true', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return public_account(account)


@router.get('/options')
def options():
    if os.getenv('SUIXINJI_CONSOLE_ENABLED') != '1':
        raise HTTPException(404)
    return {'mode': 'accounts' if accounts_enabled() else 'token',
            'registration': accounts_enabled() and os.getenv('SUIXINJI_CONSOLE_REGISTRATION_ENABLED') == 'true'}


@router.post('/register', status_code=201)
def register(body: Registration, request: Request, response: Response):
    enabled()
    check_mutation(request)
    if os.getenv('SUIXINJI_CONSOLE_REGISTRATION_ENABLED') != 'true':
        raise HTTPException(403, '暂未开放注册，请联系管理员')
    limit_auth(request, body.username)
    try:
        account = store.create_account(body.username, body.password, body.alias)
    except IntegrityError:
        raise HTTPException(409, '该账号无法注册，请换一个用户名') from None
    return start_session(request, response, account)


@router.post('/login')
def login(body: Credentials, request: Request, response: Response):
    enabled()
    check_mutation(request)
    limit_auth(request, body.username)
    account = store.find_account(body.username)
    valid = store.verify_password(body.password, account['password_hash'] if account else DUMMY_HASH)
    if not valid or not account:
        raise HTTPException(401, '用户名或密码错误')
    return start_session(request, response, account)


@router.get('/me')
def me(request: Request, response: Response):
    account = authenticated(request)
    response.headers['Cache-Control'] = 'no-store'
    return public_account(account)


@router.post('/logout')
def logout(request: Request, response: Response):
    enabled()
    check_mutation(request)
    store.revoke_session(request.cookies.get(COOKIE, ''))
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


@router.post('/password')
def password(body: PasswordChange, request: Request, response: Response):
    account = authenticated(request)
    limit_auth(request, account['username'])
    if not store.verify_password(body.current_password, account['password_hash']):
        raise HTTPException(401, '当前密码错误')
    store.change_password(account['id'], body.new_password)
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}
