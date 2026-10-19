"""Local message console, authentic distributed workers and durable observations."""
from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from core.lifecycle import resume, span, safe
from repositories.postgres import lifecycle as store

router = APIRouter()
STATIC = Path(__file__).parent / 'console_static'


def authorize(authorization: str | None = Header(default=None)):
    if os.getenv('SUIXINJI_CONSOLE_ENABLED') != '1':
        raise HTTPException(404, 'console disabled')
    token = os.getenv('SUIXINJI_CONSOLE_TOKEN', '')
    if not token:
        raise HTTPException(503, 'console token not configured')
    if not hmac.compare_digest(authorization or '', 'Bearer '+token):
        raise HTTPException(401, '请输入有效的控制台访问令牌')
    return os.getenv('SUIXINJI_CONSOLE_TENANT', 'default')


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=24000)
    mode: Literal['auto', 'ingest', 'ask'] = 'auto'
    space_id: str = Field(default='console', min_length=1, max_length=200, pattern=r'^[\w:.-]+$')
    user_id: str = Field(default='console-user', min_length=1, max_length=200)


class AliasInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    alias: str = Field(min_length=1, max_length=40)

    @field_validator('alias')
    @classmethod
    def clean_alias(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError('化名不能为空或包含换行等控制字符')
        return value


@router.get('/api/console/profile')
def profile(user_id: str, tenant=Depends(authorize)):
    from repositories.postgres.user_profile import get_alias
    return {'user_id': user_id, 'alias': get_alias(user_id, tenant_id=tenant)}


@router.put('/api/console/profile')
def update_profile(request: AliasInput, tenant=Depends(authorize)):
    from repositories.postgres.user_profile import save_alias
    try:
        save_alias(request.user_id, request.alias, tenant_id=tenant)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'user_id': request.user_id, 'alias': request.alias}


@router.get('/console', include_in_schema=False)
def page():
    if os.getenv('SUIXINJI_CONSOLE_ENABLED') != '1':
        raise HTTPException(404)
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control':'no-store'})


@router.get('/console/assets/{name}', include_in_schema=False)
def asset(name: str):
    if name not in {'app.js','style.css'}:
        raise HTTPException(404)
    return FileResponse(STATIC / name)


@router.get('/api/console/config')
def config(tenant=Depends(authorize)):
    from core import settings as s
    from infrastructure.database import get_engine
    from infrastructure.redis_client import get_redis
    from sqlalchemy import text
    dependencies={}
    for name,check in [('postgres',lambda:get_engine().connect()), ('redis',lambda:get_redis().ping())]:
        try:
            result=check()
            if name=='postgres':
                with result as conn:
                    conn.execute(text('select 1'))
            dependencies[name]='connected'
        except Exception as exc:
            dependencies[name]=type(exc).__name__
    return {'tenant_id':tenant, 'dependencies':dependencies, 'fake_externals':s.FAKE_EXTERNALS,
            'ask_v2':s.ASK_V2_ENABLED, 'ask_v2_shadow':s.ASK_V2_SHADOW, 'extractor':s.MEMORY_EXTRACTOR_MODE, 'storage':s.STORAGE_BACKEND,
            'queue':s.TASK_QUEUE_BACKEND, 'preference_cards':s.PREFERENCE_CARDS_ENABLED}


@router.post('/api/console/messages', status_code=202)
def send(request: Message, tenant=Depends(authorize)):
    from apps.receiver import InboxCommand, receive
    from apps.api import ReceiveRequest, TestApiContext, _check_rate_limit
    from core import settings as s
    if not request.text.strip():
        raise HTTPException(422, '消息不能为空')
    if s.STORAGE_BACKEND != 'postgres' or s.TASK_QUEUE_BACKEND != 'redis_streams':
        raise HTTPException(503, '控制台需要 PostgreSQL 与 Redis Streams')
    from apps.console_commands import route_message
    from core.sensitive import assess_sensitive_text
    sensitive=assess_sensitive_text(request.text)
    route=route_message(request.text,request.mode) if not sensitive.blocks_storage else {'task_type':'query','mode':'rejected','payload':{}}
    mode=route['task_type']
    trace_id=uuid.uuid4().hex
    message_id='console-'+trace_id
    store.create_run(id=trace_id,tenant_id=tenant,space_id=request.space_id,message_id=message_id,
                     mode=route['mode'],text='[敏感内容已拦截]' if sensitive.blocks_storage else safe(request.text),config_json={'fake_externals':s.FAKE_EXTERNALS,'ask_v2':s.ASK_V2_ENABLED,'extractor':s.MEMORY_EXTRACTOR_MODE,'preference_cards':s.PREFERENCE_CARDS_ENABLED})
    if sensitive.blocks_storage:
        with resume({'trace_id':trace_id}), span('敏感内容拦截', 'ingress', {'category':sensitive.category}) as observation:
            reply='检测到疑似密码、密钥或高风险身份信息：内容未保存到业务表，也未发送给模型。'
            store.update_run(trace_id,answer=reply)
            observation['output']={'rejected':True,'reason':sensitive.reason}
        return {'trace_id':trace_id,'task_id':None}
    try:
        with resume({'trace_id':trace_id}), span('FastAPI · 接收消息','ingress', request.model_dump()) as observation:
            with span('Redis · 入口限流', 'queue', {'user_id':request.user_id,'task_type':mode}) as limit_observation:
                _check_rate_limit(ReceiveRequest(message_id=message_id, space_id=request.space_id,text=request.text,task_type=mode),TestApiContext(tenant, request.user_id))
                limit_observation['output']={'allowed':True}
            route=route_message(request.text,request.mode)
            result=receive(InboxCommand(source='console',message_id=message_id,tenant_id=tenant,
                space_id=request.space_id,text=request.text,task_type=mode,chat_id=f'console:{trace_id}',
                task_payload={'question':request.text,'user_id':request.user_id,'reply_channel':'console',**route['payload']},
                sender={'user_id':request.user_id}))
            store.update_run(trace_id,task_id=result.task_id)
            observation['output']=result
    except HTTPException as exc:
        store.update_run(trace_id,error=str(exc.detail))
        raise
    except Exception as exc:
        store.update_run(trace_id,error=safe(f'{type(exc).__name__}: {exc}'))
        raise HTTPException(503, {'message':'消息提交失败，可在历史中查看详情','trace_id':trace_id}) from exc
    return {'trace_id':trace_id,'task_id':result.task_id}


@router.get('/api/console/traces')
def history(tenant=Depends(authorize)):
    return store.list_runs(tenant)


@router.get('/api/console/traces/{trace_id}')
def trace(trace_id:str,tenant=Depends(authorize)):
    result=store.detail(trace_id,tenant)
    if result is None:
        raise HTTPException(404,'trace not found')
    return result


@router.get('/api/console/commands')
def commands(tenant=Depends(authorize)):
    from apps.console_commands import COMMANDS
    return COMMANDS
