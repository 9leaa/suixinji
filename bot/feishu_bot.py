"""Feishu bot receiver for the Suixinji Agent P1 ingestion pipeline."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from apps.receiver import InboxCommand, receive
from agent.query_agent import by_tag, by_type, filter_notes
from summary.daily_summary import parse_summary_range
from summary.scheduler import start_summary_scheduler
from summary.subscription import (
    disable_summary_subscription,
    enable_summary_subscription,
    get_summary_subscription,
    update_summary_time,
)
from dotenv import load_dotenv
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)
from core.feedback import save_feedback
from core.observability import latest_success, log_event, recent_errors
from core.sensitive import assess_sensitive_text, safe_text_preview
from core.settings import TASK_QUEUE_BACKEND
from core.wal import (
    append_message_once,
    create_blocked_sensitive_record,
    create_pending_record,
    list_wal_space_ids,
    load_pending_records,
)
from core.worker import process_pending
from memory.service import (
    format_memory_approve,
    format_memory_conflicts,
    format_memory_consolidate,
    format_memory_correct,
    format_memory_decisions,
    format_memory_edit,
    format_memory_forget,
    format_memory_list,
    format_memory_pending,
    format_memory_profile,
    format_memory_purge,
    format_memory_reject,
    format_memory_resolve,
    format_memory_search,
    format_memory_show,
    format_memory_stats,
    format_trace_id,
    format_trace_latest,
    format_trace_memory,
)
from memory.scheduler import start_memory_scheduler
from runtime.delivery_store import manual_summary_key, query_key, recover_stale_reserved_deliveries
from runtime.enrichment_drainer import EnrichmentDrainer
from runtime.executor import get_task_executor
from runtime.pending_drainer import PendingDrainer
from runtime.task import TASK_REJECTED


load_dotenv()

LOGGER = logging.getLogger(__name__)

APP_ID = os.getenv("FEISHU_APP_ID", "")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "")
VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "")

_lark_client: Any | None = None


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a value from either an SDK model object or a dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _get_first_attr(obj: Any, names: tuple[str, ...], default: Any = None) -> Any:
    """Read the first non-empty attribute from an SDK model object or a dict."""
    for name in names:
        value = _get_attr(obj, name)
        if value not in (None, ""):
            return value
    return default


def require_feishu_config() -> None:
    """Ensure the required Feishu app credentials exist before starting."""
    missing = []
    if not APP_ID:
        missing.append("FEISHU_APP_ID")
    if not APP_SECRET:
        missing.append("FEISHU_APP_SECRET")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def get_lark_client() -> Any:
    """Create and cache the Feishu OpenAPI client."""
    global _lark_client
    if _lark_client is None:
        require_feishu_config()
        _lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
    return _lark_client


def send_text(chat_id: str, text: str) -> None:
    """Send a text message to a Feishu chat by chat_id."""
    content = json.dumps({"text": text}, ensure_ascii=False)
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(content)
            .build()
        )
        .build()
    )

    response = get_lark_client().im.v1.message.create(request)
    if not response.success():
        raise RuntimeError(
            "client.im.v1.message.create failed, "
            f"code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}"
        )


def safe_send_text(chat_id: str, text: str) -> bool:
    """Try to send a text message without breaking the ingestion path."""
    try:
        send_text(chat_id, text)
        return True
    except Exception:
        LOGGER.exception("Failed to send Feishu text message")
        return False


def parse_text_content(content: Any) -> str:
    """Extract plain text from Feishu message.content."""
    if isinstance(content, dict):
        return str(content.get("text", ""))

    if not content:
        return ""

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        LOGGER.warning("Failed to decode Feishu text content as JSON: content_len=%s", len(str(content)))
        return ""

    return str(data.get("text", ""))


def strip_bot_mentions(text: str, message: Any) -> str:
    """Remove leading bot mentions from group-chat text messages."""
    mentions = _get_attr(message, "mentions", []) or []
    for mention in mentions:
        key = _get_attr(mention, "key")
        if key:
            text = text.replace(str(key), "")

    text = re.sub(r"^\s*<at\b[^>]*>.*?</at>\s*", "", text)
    return text.strip()


def extract_sender(event: Any) -> dict[str, Any]:
    """Extract sender fields from a Feishu message event."""
    sender = _get_attr(event, "sender")
    sender_id = _get_attr(sender, "sender_id")

    return {
        "sender_type": _get_attr(sender, "sender_type"),
        "tenant_key": _get_attr(sender, "tenant_key"),
        "open_id": _get_first_attr(sender_id, ("open_id", "openId")),
        "user_id": _get_first_attr(sender_id, ("user_id", "userId")),
        "union_id": _get_first_attr(sender_id, ("union_id", "unionId")),
    }


def build_space_id(chat_type: str, chat_id: str | None, sender: dict[str, Any]) -> str:
    """Build the internal space_id used for WAL and note storage."""
    open_id = sender.get("open_id")
    if chat_type == "p2p" and open_id:
        return f"p_{open_id}"
    if chat_id:
        return f"g_{chat_id}"
    if open_id:
        return f"p_{open_id}"
    raise ValueError("Cannot build space_id without chat_id or sender.open_id")


def _parse_limit(value: Any, default: int) -> int:
    try:
        return max(1, min(int(value), 100))
    except (TypeError, ValueError):
        return default


def _split_tags(value: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,，、]", value)
        if item.strip()
    ]


def _format_note_results(notes: list[dict[str, Any]], title: str, limit: int = 8) -> str:
    if not notes:
        return f"{title}：没有找到匹配的笔记。"

    shown = notes[:limit]
    lines = [f"{title}：找到 {len(notes)} 条，显示前 {len(shown)} 条。"]
    for note in shown:
        date = str(note.get("time") or "")[:10] or "无日期"
        tags = " ".join(f"#{tag}" for tag in note.get("tags", [])) or "无标签"
        summary = note.get("summary") or note.get("text") or ""
        lines.append(
            f"- {date}｜{note.get('title') or '无标题'}｜{note.get('type') or '无类型'}｜{tags}\n  {summary}"
        )

    return "\n".join(lines)


def _parse_filter_args(raw: str) -> dict[str, str]:
    args: dict[str, str] = {}
    for part in raw.split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            args[key] = value
    return args

def _format_summary_auto_status(space_id: str) -> str:
    sub = get_summary_subscription(space_id)
    if sub is None:
        return "自动总结：未开启。"

    status = "开启" if sub.enabled else "关闭"
    last_sent = sub.last_sent_date or "尚未发送"
    return (
        f"自动总结：{status}\n"
        f"- 时间：{sub.time}\n"
        f"- 范围：今天\n"
        f"- 最近发送：{last_sent}"
    )


def _clip_status_text(value: Any, limit: int = 80) -> str:
    return safe_text_preview(str(value or ""), limit=limit)


def _format_system_status(space_id: str) -> str:
    pending_count = len(load_pending_records(space_id))
    sub = get_summary_subscription(space_id)
    auto_status = "未开启"
    if sub is not None:
        auto_status = f"{'开启' if sub.enabled else '关闭'}，时间 {sub.time}，最近发送 {sub.last_sent_date or '尚未发送'}"

    last_success = latest_success({
        "worker.process_record",
        "query.answer_question",
        "feishu.command.summary",
        "summary.auto.send",
    })
    last_success_text = "暂无"
    if last_success is not None:
        last_success_text = f"{last_success.get('ts')}｜{last_success.get('action')}"

    errors = recent_errors(limit=3)
    task_stats = get_task_executor(safe_send_text).get_stats()
    lines = [
        "系统状态：",
        f"- 当前会话 pending：{pending_count} 条",
        f"- 运行任务：{task_stats['running']} 个",
        f"- 后台 LLM 增强：{task_stats['inflight_enrichment']} 个",
        f"- 排队任务：{task_stats['queued']} 个",
        f"- 成功任务：{task_stats['success']} 个",
        f"- 失败任务：{task_stats['failed']} 个",
        f"- 被拒绝任务：{task_stats['rejected']} 个",
        f"- 队列容量：{task_stats['capacity']} 个",
        f"- 剩余可提交槽位：{task_stats['remaining_slots']} 个",
        f"- 最近保留任务：{task_stats['retained_tasks']} 个",
        f"- 最老排队等待：{task_stats['oldest_queued_wait_seconds']} 秒",
        f"- 最近 LLM 超时：{task_stats['last_llm_timeout_at'] or '暂无'}",
        f"- 自动总结：{auto_status}",
        f"- 最近成功：{last_success_text}",
        f"- 最近错误：{len(errors)} 条",
        "- 日志目录：data/logs/",
    ]

    for item in errors:
        lines.append(
            f"  - {item.get('ts')}｜{item.get('action')}｜{_clip_status_text(item.get('error'))}"
        )

    return "\n".join(lines)


def _handle_summary_auto_command(space_id: str, chat_id: str, text: str) -> str | None:
    if not (text == "/summary_auto" or text.startswith("/summary_auto ")):
        return None

    raw = text.removeprefix("/summary_auto").strip()
    if not raw:
        return "用法：/summary_auto on｜off｜status｜time 22:00"

    parts = raw.split()
    action = parts[0].lower()

    if action == "on":
        sub = enable_summary_subscription(space_id, chat_id)
        return f"已开启自动总结，每天 {sub.time} 推送今天的随心记总结。"

    if action == "off":
        sub = disable_summary_subscription(space_id)
        if sub is None:
            return "自动总结本来就没有开启。"
        return "已关闭自动总结。"

    if action == "status":
        return _format_summary_auto_status(space_id)

    if action == "time":
        if len(parts) < 2:
            return "用法：/summary_auto time 22:00"
        try:
            sub = update_summary_time(space_id, chat_id, parts[1])
        except ValueError:
            return "时间格式不对，请使用 HH:MM，例如：/summary_auto time 22:00"
        return f"自动总结时间已改为每天 {sub.time}。"

    return "用法：/summary_auto on｜off｜status｜time 22:00"

def _handle_direct_query_command(space_id: str, text: str) -> str | None:
    if text.startswith("/type"):
        raw = text.removeprefix("/type").strip()
        if not raw:
            return "用法：/type 生活"
        parts = raw.split()
        note_type = parts[0]
        limit = _parse_limit(parts[1], 30) if len(parts) > 1 else 30
        return _format_note_results(
            by_type(space_id, note_type, limit=limit),
            f"type={note_type} 的笔记",
        )

    if text.startswith("/tag"):
        raw = text.removeprefix("/tag").strip()
        if not raw:
            return "用法：/tag 饮食"
        parts = raw.split()
        tag = parts[0]
        limit = _parse_limit(parts[1], 10) if len(parts) > 1 else 10
        return _format_note_results(
            by_tag(space_id, tag, limit=limit),
            f"tag={tag} 的笔记",
        )

    if text.startswith("/filter"):
        raw = text.removeprefix("/filter").strip()
        if not raw:
            return "用法：/filter type=生活 tags=饮食,日常"

        args = _parse_filter_args(raw)
        note_type = args.get("type") or args.get("note_type")
        tags = _split_tags(args.get("tags") or args.get("tag") or "")
        if not note_type and not tags:
            return "用法：/filter type=生活 tags=饮食,日常"

        match = (args.get("match") or args.get("match_all_tags") or "all").lower()
        match_all_tags = match not in {"any", "or", "false", "0", "任一"}
        limit = _parse_limit(args.get("limit"), 30)
        return _format_note_results(
            filter_notes(
                space_id,
                note_type=note_type,
                tags=tags,
                match_all_tags=match_all_tags,
                limit=limit,
            ),
            "筛选结果",
        )

    return None


def _handle_memory_command(space_id: str, text: str) -> str | None:
    if not (text == "/memory" or text.startswith("/memory ")):
        return None

    raw = text.removeprefix("/memory").strip()
    if not raw:
        return "用法：/memory list｜show <id>｜search <内容>｜profile｜pending｜approve <id>｜reject <id>｜edit <id> <内容>｜resolve <id> keep|merge|archive [内容]｜decisions｜forget <id>｜purge <id>｜correct <id> <新内容>｜conflicts｜stats｜consolidate daily|weekly|monthly"

    parts = raw.split(maxsplit=2)
    action = parts[0].lower()

    if action == "list":
        return format_memory_list(space_id)
    if action == "show" and len(parts) >= 2:
        return format_memory_show(parts[1])
    if action == "search" and len(parts) >= 2:
        query = raw.removeprefix("search").strip()
        return format_memory_search(space_id, query)
    if action == "forget" and len(parts) >= 2:
        return format_memory_forget(parts[1])
    if action == "purge" and len(parts) >= 2:
        return format_memory_purge(parts[1])
    if action == "correct" and len(parts) >= 3:
        return format_memory_correct(parts[1], parts[2])
    if action == "reject" and len(parts) >= 2:
        return format_memory_reject(parts[1], parts[2] if len(parts) >= 3 else "user_rejected_pending_memory")
    if action == "edit" and len(parts) >= 3:
        return format_memory_edit(parts[1], parts[2])
    if action == "resolve" and len(parts) >= 3:
        resolution_parts = parts[2].split(maxsplit=1)
        return format_memory_resolve(parts[1], resolution_parts[0], resolution_parts[1] if len(resolution_parts) == 2 else None)
    if action == "conflicts":
        return format_memory_conflicts(space_id)
    if action == "pending":
        return format_memory_pending(space_id)
    if action == "approve" and len(parts) >= 2:
        return format_memory_approve(parts[1])
    if action == "decisions":
        return format_memory_decisions(space_id)
    if action == "profile":
        return format_memory_profile(space_id)
    if action == "stats":
        return format_memory_stats(space_id)
    if action == "consolidate" and len(parts) >= 2:
        return format_memory_consolidate(space_id, parts[1])

    return "用法：/memory list｜show <id>｜search <内容>｜profile｜pending｜approve <id>｜reject <id>｜edit <id> <内容>｜resolve <id> keep|merge|archive [内容]｜decisions｜forget <id>｜purge <id>｜correct <id> <新内容>｜conflicts｜stats｜consolidate daily|weekly|monthly"


def _handle_trace_command(text: str) -> str | None:
    if not (text == "/trace" or text.startswith("/trace ")):
        return None

    raw = text.removeprefix("/trace").strip()
    if not raw or raw == "latest":
        return format_trace_latest()
    if raw.startswith("memory "):
        memory_id = raw.removeprefix("memory").strip()
        if not memory_id:
            return "用法：/trace memory <memory_id>"
        return format_trace_memory(memory_id)
    return format_trace_id(raw)

def handle_text_message(data: P2ImMessageReceiveV1) -> None:
    """Handle a Feishu im.message.receive_v1 event."""
    event = data.event
    header = _get_attr(data, "header")
    message = event.message

    message_type = _get_attr(message, "message_type")
    chat_id = _get_attr(message, "chat_id")
    chat_type = _get_attr(message, "chat_type") or "p2p"
    message_id = _get_attr(message, "message_id")
    event_id = _get_attr(header, "event_id")
    sender = extract_sender(event)

    if not chat_id:
        LOGGER.warning("Skip message without chat_id: %s", lark.JSON.marshal(data))
        log_event(
            "feishu.message.received",
            level="warning",
            status="skipped",
            message_id=message_id,
            extra={"reason": "missing_chat_id", "message_type": message_type},
        )
        return

    if message_type != "text":
        log_event(
            "feishu.message.received",
            status="skipped",
            message_id=message_id,
            extra={"reason": "unsupported_message_type", "message_type": message_type, "chat_type": chat_type},
        )
        safe_send_text(chat_id, "暂时只支持文本消息，语音、图片和文件会在后续阶段接入。")
        return

    text = parse_text_content(_get_attr(message, "content"))
    text = strip_bot_mentions(text, message)
    if not text:
        log_event(
            "feishu.message.received",
            status="skipped",
            message_id=message_id,
            extra={"reason": "empty_text", "chat_type": chat_type},
        )
        safe_send_text(chat_id, "收到空文本，已跳过。")
        return

    space_id = build_space_id(chat_type, chat_id, sender)
    log_event(
        "feishu.message.received",
        status="success",
        space_id=space_id,
        message_id=message_id,
        extra={"message_type": message_type, "chat_type": chat_type, "text_len": len(text)},
    )

    sensitive = assess_sensitive_text(text)
    if sensitive.blocks_storage:
        record = create_blocked_sensitive_record(
            message_id=str(message_id or event_id or "unknown"),
            space_id=space_id,
            category=str(sensitive.category or "sensitive"),
            event_id=event_id,
            chat_id=chat_id,
            chat_type=chat_type,
            sender=sender,
        )
        appended = append_message_once(record)
        log_event(
            "feishu.sensitive_rejected",
            level="warning",
            status="rejected" if appended else "skipped",
            space_id=space_id,
            message_id=message_id,
            record_id=record.id if appended else None,
            extra={
                "category": sensitive.category,
                "reason": sensitive.reason,
                "duplicate": not appended,
            },
        )
        if appended:
            safe_send_text(chat_id, "检测到疑似密码、密钥或高风险身份信息：这条内容未保存，也未发送给模型。")
        else:
            safe_send_text(chat_id, "这条敏感消息已经拦截过了，未重复处理。")
        return

    summary_auto_reply = _handle_summary_auto_command(space_id, chat_id, text)
    if summary_auto_reply is not None:
        safe_send_text(chat_id, summary_auto_reply)
        return

    direct_query_reply = _handle_direct_query_command(space_id, text)
    if direct_query_reply is not None:
        safe_send_text(chat_id, direct_query_reply)
        return

    memory_reply = _handle_memory_command(space_id, text)
    if memory_reply is not None:
        safe_send_text(chat_id, memory_reply)
        return

    trace_reply = _handle_trace_command(text)
    if trace_reply is not None:
        safe_send_text(chat_id, trace_reply)
        return

    if text == "/status":
        log_event("feishu.command.status", status="start", space_id=space_id, message_id=message_id)
        reply = _format_system_status(space_id)
        sent = safe_send_text(chat_id, reply)
        if sent:
            log_event(
                "feishu.command.status",
                status="success",
                space_id=space_id,
                message_id=message_id,
                extra={"reply_len": len(reply)},
            )
        else:
            log_event(
                "feishu.command.status",
                level="error",
                status="failed",
                space_id=space_id,
                message_id=message_id,
                error="safe_send_text returned False",
                extra={"reply_len": len(reply)},
            )
        return

    if text == "/feedback" or text.startswith("/feedback "):
        feedback_text = text.removeprefix("/feedback").strip()
        if not feedback_text:
            log_event(
                "feishu.command.feedback",
                level="warning",
                status="skipped",
                space_id=space_id,
                message_id=message_id,
                extra={"reason": "missing_text"},
            )
            safe_send_text(chat_id, "用法：/feedback 这次总结漏了健身计划")
            return

        record = save_feedback(
            space_id=space_id,
            message_id=message_id,
            text=feedback_text,
        )

        log_event(
            "feishu.command.feedback",
            status="success",
            space_id=space_id,
            message_id=message_id,
            record_id=record.id,
            extra={"text_len": len(feedback_text)},
        )
        safe_send_text(chat_id, "已记录反馈，我会把它用于后续改进。")
        return

    if text == "/summary" or text.startswith("/summary "):
        log_event("feishu.command.summary", status="start", space_id=space_id, message_id=message_id)
        raw = text.removeprefix("/summary").strip()
        if not raw:
            log_event(
                "feishu.command.summary",
                level="warning",
                status="skipped",
                space_id=space_id,
                message_id=message_id,
                extra={"reason": "missing_range"},
            )
            safe_send_text(chat_id, "用法：/summary 今天｜昨天｜一周｜一个月｜半年｜一年")
            return

        range_key = parse_summary_range(raw)
        if range_key is None:
            log_event(
                "feishu.command.summary",
                level="warning",
                status="skipped",
                space_id=space_id,
                message_id=message_id,
                extra={"reason": "invalid_range", "raw": raw},
            )
            safe_send_text(chat_id, "暂不支持这个总结范围。可用：今天、昨天、一周、一个月、半年、一年")
            return

        safe_send_text(chat_id, "我来整理这段时间的随心记。")
        if TASK_QUEUE_BACKEND == "redis_streams":
            result = receive(
                InboxCommand(
                    source="feishu",
                    message_id=message_id,
                    event_id=event_id,
                    tenant_id=str(sender.get("tenant_key") or "default"),
                    space_id=space_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    sender=sender,
                    text=text,
                    task_type="summary",
                    task_payload={
                        "range_key": range_key,
                        "chat_id": chat_id,
                        "delivery_key": manual_summary_key(space_id, message_id),
                        "delivery_type": "manual_summary",
                    },
                )
            )
            if result.duplicate:
                safe_send_text(chat_id, "这条总结请求已经收到过了。")
            return
        task = get_task_executor(safe_send_text).submit_summary(
            space_id,
            range_key,
            chat_id,
            message_id,
        )
        if task.status == TASK_REJECTED:
            safe_send_text(chat_id, "当前任务较多，请稍后重试。")
        return

    if text.startswith("/ask"):
        question = text.removeprefix("/ask").strip()
        log_event(
            "feishu.command.ask",
            status="start",
            space_id=space_id,
            message_id=message_id,
            extra={"question_len": len(question)},
        )
        if not question:
            log_event(
                "feishu.command.ask",
                level="warning",
                status="skipped",
                space_id=space_id,
                message_id=message_id,
                extra={"reason": "missing_question"},
            )
            safe_send_text(chat_id, "用法：/ask 上次说的那家咖啡店在哪")
            return

        safe_send_text(chat_id, "我去翻一下随心记。")
        if TASK_QUEUE_BACKEND == "redis_streams":
            result = receive(
                InboxCommand(
                    source="feishu",
                    message_id=message_id,
                    event_id=event_id,
                    tenant_id=str(sender.get("tenant_key") or "default"),
                    space_id=space_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    sender=sender,
                    text=text,
                    task_type="query",
                    task_payload={
                        "question": question,
                        "chat_id": chat_id,
                        "delivery_key": query_key(space_id, message_id),
                        "delivery_type": "query",
                    },
                )
            )
            if result.duplicate:
                safe_send_text(chat_id, "这条查询已经收到过了。")
            return
        task = get_task_executor(safe_send_text).submit_query(
            space_id,
            question,
            chat_id,
            message_id,
        )
        if task.status == TASK_REJECTED:
            safe_send_text(chat_id, "当前任务较多，请稍后重试。")
        return

    if TASK_QUEUE_BACKEND == "redis_streams":
        result = receive(
            InboxCommand(
                source="feishu",
                message_id=message_id,
                event_id=event_id,
                tenant_id=str(sender.get("tenant_key") or "default"),
                space_id=space_id,
                chat_id=chat_id,
                chat_type=chat_type,
                sender=sender,
                text=text,
                task_type="ingest",
                task_payload={"chat_id": chat_id, "notify_on_success": True, "source": "feishu_realtime"},
            )
        )
        if result.duplicate:
            safe_send_text(chat_id, "这条消息已经收到过了，已跳过重复处理。")
        else:
            safe_send_text(chat_id, "已收到，正在整理到随心记。")
        return

    record = create_pending_record(
        message_id=message_id,
        space_id=space_id,
        text=text,
        event_id=event_id,
        chat_id=chat_id,
        chat_type=chat_type,
        sender=sender,
    )

    appended = append_message_once(record)
    if not appended:
        safe_send_text(chat_id, "这条消息已经收到过了，已跳过重复处理。")
        return

    safe_send_text(chat_id, "已收到，正在整理到随心记。")
    task = get_task_executor(safe_send_text).submit_ingest(
        record,
        chat_id,
        notify_on_success=True,
        source="feishu_realtime",
    )
    if task.status == TASK_REJECTED:
        safe_send_text(chat_id, "消息已保存到 WAL，当前任务较多，稍后会从 pending 记录继续处理。")


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    """Feishu SDK callback for the message receive v2.0 event."""
    try:
        handle_text_message(data)
    except Exception:
        LOGGER.exception("Unhandled Feishu message event error")


def build_event_handler() -> Any:
    """Build the Feishu event dispatcher for long-connection mode."""
    return (
        lark.EventDispatcherHandler.builder(ENCRYPT_KEY, VERIFICATION_TOKEN)
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .build()
    )


def recover_pending_records() -> None:
    """Recover pending WAL records before receiving new Feishu events."""
    space_ids = list_wal_space_ids()
    if not space_ids:
        LOGGER.info("No WAL files found for startup recovery")
        return

    for space_id in space_ids:
        try:
            count = process_pending(space_id)
        except Exception:
            LOGGER.exception("Failed to recover pending records for space_id=%s", space_id)
            continue

        if count:
            LOGGER.info("Recovered %s pending WAL record(s) for space_id=%s", count, space_id)


def start() -> None:
    """Start the Feishu long-connection client and block the current process."""
    require_feishu_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    if TASK_QUEUE_BACKEND == "local":
        executor = get_task_executor(safe_send_text)
        recover_stale_reserved_deliveries()
        pending_drainer = PendingDrainer(executor)
        pending_drainer.drain_once()
        pending_drainer.start()
        enrichment_drainer = EnrichmentDrainer(executor)
        enrichment_drainer.drain_once()
        enrichment_drainer.start()
        start_summary_scheduler(safe_send_text, executor=executor)
        start_memory_scheduler()

    ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=build_event_handler(),
        # INFO includes the temporary WebSocket access URL and ticket.  Keep
        # third-party transport logs at WARNING so credentials never land in
        # runtime.log during a normal connection.
        log_level=lark.LogLevel.WARNING,
    )
    LOGGER.info("Starting Feishu long-connection client...")
    ws_client.start()


if __name__ == "__main__":
    start()
