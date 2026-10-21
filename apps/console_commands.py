"""Console command adapter. Reuses existing command services, never sends Feishu text."""
from __future__ import annotations
from core.lifecycle import traced, span

COMMANDS = [
    {'command':'普通消息','example':'我喜欢喝无糖咖啡。','description':'保存笔记、提取与更新记忆'},
    {'command':'任务清单','example':'当前还有哪些未完成任务？列举出来。','description':'完整列出当前用户已保存的未完成任务'},
    {'command':'完成任务','example':'任务1、任务5请标记完成','description':'按最近一次清单编号更新原任务'},
    {'command':'/ask','example':'/ask 我喜欢喝什么？','description':'自然语言查询与多路召回'},
    {'command':'/memory consolidate preferences','example':'/memory consolidate preferences','description':'审核偏好原文、整理细节与单向关联'},
    {'command':'/type','example':'/type 生活 10','description':'按类型查询笔记'},
    {'command':'/tag','example':'/tag 饮食 10','description':'按标签查询笔记'},
    {'command':'/filter','example':'/filter type=生活 tags=饮食 match=any limit=10','description':'组合筛选笔记'},
    {'command':'/summary','example':'/summary 今天','description':'总结：今天、昨天、一周、一个月、半年、一年'},
    {'command':'/summary_auto','example':'/summary_auto status','description':'自动总结：on / off / status / time 22:00'},
    *[{'command':'/memory '+name,'example':'/memory '+example,'description':description} for name,example,description in [
        ('list','list','列出记忆'),('show','show <id>','查看记忆及版本'),('search','search 饮品偏好','检索记忆'),
        ('profile','profile','用户画像'),('pending','pending','待审核记忆'),('approve','approve <id>','批准记忆'),
        ('reject','reject <id> 原因','拒绝候选'),('edit','edit <id> 新内容','编辑候选'),
        ('resolve','resolve <id> keep','解决冲突：keep / merge / archive'),('decisions','decisions','裁决记录'),
        ('forget','forget <id>','遗忘记忆'),('purge','purge <id>','永久清除记忆'),
        ('correct','correct <id> 新内容','更正记忆，可用 --status done'),('conflicts','conflicts','查看冲突'),
        ('stats','stats','记忆统计'),('consolidate','consolidate daily','合并整理：daily / weekly / monthly')]],
    {'command':'/trace','example':'/trace latest','description':'当前空间的 latest / <trace_id> / memory <memory_id>'},
    {'command':'/status','example':'/status','description':'系统状态'},
    {'command':'/feedback','example':'/feedback 这次回答漏掉了一条记录','description':'记录反馈'},
    {'command':'/help','example':'/help','description':'显示完整命令列表'},
]


@traced('命令识别与路由', 'planning')
def route_message(text: str, mode='auto'):
    text=text.strip()
    from core import settings
    if settings.PREFERENCE_CARDS_ENABLED and text.startswith('/pending '):
        return {'task_type': 'ingest', 'mode': 'ingest', 'payload': {}}
    from memory.task_commands import parse_command
    if settings.TASK_CARDS_ENABLED and parse_command(text):
        return {'task_type': 'query', 'mode': 'task', 'payload': {'question': text, 'consistency': 'memory'}}
    if mode=='ask' and not text.startswith('/'):
        text='/ask '+text
    if not text.startswith('/'):
        return {'task_type':'ingest','mode':'ingest','payload':{}}
    parts=text.split(maxsplit=1)
    head,tail=parts[0], parts[1] if len(parts)>1 else ''
    if head=='/ask' and tail.strip():
        return {'task_type':'query','mode':'ask','payload':{'question':tail.strip()}}
    if head=='/summary':
        from summary.daily_summary import parse_summary_range
        range_key=parse_summary_range(tail.strip())
        if range_key:
            return {'task_type':'summary','mode':'summary','payload':{'range_key':range_key}}
    consistency='memory' if head=='/memory' else 'note' if head in {'/type','/tag','/filter','/summary'} else 'weak'
    return {'task_type':'query','mode':head[:20], 'payload':{'console_command':text,'consistency':consistency}}


def _check_memory_scope(space_id, memory_id):
    from memory.repository import get_memory
    record=get_memory(memory_id)
    if record is None or record.space_id!=space_id:
        raise ValueError('该记忆不属于当前空间或不存在')


@traced('命令执行', 'command')
def execute_command(space_id, text, *, tenant_id='default', message_id=None):
    from bot import feishu_bot as existing
    parts=text.strip().split(maxsplit=1)
    head,tail=parts[0], parts[1] if len(parts)>1 else ''
    if head=='/help':
        return '\n'.join(c['example']+' — '+c['description'] for c in COMMANDS)
    if head in {'/type','/tag','/filter'}:
        return existing._handle_direct_query_command(space_id,text)
    if head=='/memory':
        if tail.strip() in {'consolidate', 'consolidate preferences'}:
            from memory.preference_cards import run_consolidation
            import json
            return json.dumps(run_consolidation(space_id, force=True), ensure_ascii=False, indent=2)
        parts=tail.split(maxsplit=2)
        if parts and parts[0] in {'show','forget','purge','correct','reject','edit','resolve','approve'} and len(parts)>1:
            _check_memory_scope(space_id,parts[1])
        return existing._handle_memory_command(space_id,text)
    if head=='/trace':
        return _scoped_trace(space_id,tail.strip())
    if head=='/summary_auto':
        # This destination is recognized by StreamTaskDispatcher; it never reaches Feishu.
        return existing._handle_summary_auto_command(space_id,'console:'+tenant_id,text)
    if head=='/status':
        return existing._format_system_status(space_id)
    if head=='/feedback':
        if not tail.strip():return '用法：/feedback 这次总结漏了健身计划'
        from core.feedback import save_feedback
        save_feedback(space_id=space_id,message_id=message_id,text=tail.strip())
        return '已记录反馈，我会把它用于后续改进。'
    if head=='/summary':return '用法：/summary 今天｜昨天｜一周｜一个月｜半年｜一年'
    if head=='/ask':return '用法：/ask 你的问题'
    return '未识别的命令。输入 /help 查看全部命令。'


@traced('读取当前空间 Trace', 'database')
def _scoped_trace(space_id,raw):
    from sqlalchemy import select
    from infrastructure.database import session_scope
    from infrastructure.schema import MemoryTrace
    from memory.service import format_trace
    statement=select(MemoryTrace).where(MemoryTrace.space_id==space_id)
    if raw.startswith('memory '):
        memory_id=raw.removeprefix('memory ').strip()
        _check_memory_scope(space_id,memory_id)
        from memory.trace import find_traces_by_memory
        traces=[r for r in find_traces_by_memory(memory_id) if r.get('space_id')==space_id]
        return '\n\n'.join(format_trace(r) for r in traces[-5:]) or '没有找到相关 trace。'
    if raw not in {'','latest'}:
        statement=statement.where(MemoryTrace.trace_id==raw)
    with session_scope() as session:
        row=session.execute(statement.order_by(MemoryTrace.started_at.desc()).limit(1)).scalar_one_or_none()
        return format_trace(dict(row.payload_json)) if row else '当前空间没有找到 trace。'


def enqueue_scheduled_summary(space_id, payload, key, message_id, max_attempts):
    """Stable run identity makes repeated scheduler ticks idempotent."""
    import hashlib
    from datetime import datetime, timezone
    from sqlalchemy.dialects.postgresql import insert
    from repositories.postgres import lifecycle as store
    from infrastructure.database import get_engine
    from repositories.postgres.dispatch import enqueue_task
    from core.lifecycle import resume
    from runtime.task import Task
    tenant=payload['chat_id'].removeprefix('console:')
    ident=hashlib.sha256((tenant+':'+key).encode()).hexdigest()[:32]
    with get_engine('lifecycle').begin() as conn:
        conn.execute(insert(store.runs).values(id=ident,tenant_id=tenant,space_id=space_id,message_id=message_id or key,
            mode='summary_auto',text='定时总结 · '+payload['range_key'],created_at=datetime.now(timezone.utc),config_json={'trigger':'scheduler'}).on_conflict_do_nothing())
    with resume({'trace_id':ident}),span('自动总结 · 定时调度', 'scheduler', payload):
        task_id,_=enqueue_task(task_type='summary',tenant_id=tenant,space_id=space_id,source_message_id=message_id,
                              idempotency_key='summary:'+key,payload=payload,max_attempts=max_attempts)
        store.update_run(ident,task_id=task_id)
    return Task(task_id,'summary',space_id,message_id,payload)
