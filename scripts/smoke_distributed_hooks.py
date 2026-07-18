"""Run a no-network-LLM Hook lifecycle smoke test and clean up its data."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, select

import agent.query_agent as query_agent
from infrastructure.database import session_scope
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import KEYS
from infrastructure.schema import AgentRun, AgentStep, LlmUsage, Space, User


def main() -> None:
    space_id = "hook-smoke-" + uuid.uuid4().hex
    query_agent.memory_search = lambda *args, **kwargs: [
        {"id": "m-smoke", "content": "用户喜欢茶", "memory_type": "preference", "sources": []}
    ]
    query_agent.complete_json = lambda **kwargs: {"final_answer": "你喜欢茶"}
    try:
        answer = query_agent.answer_question(
            space_id,
            "我喜欢什么",
            message_id="msg-" + uuid.uuid4().hex,
            user_id=space_id,
        )
        with session_scope() as session:
            report = {
                "answer": answer.splitlines()[0],
                "runs": session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.space_id == space_id)),
                "steps": session.scalar(select(func.count()).select_from(AgentStep).join(AgentRun).where(AgentRun.space_id == space_id)),
                "usage": session.scalar(select(func.count()).select_from(LlmUsage).join(AgentRun).where(AgentRun.space_id == space_id)),
            }
        print(report)
    finally:
        with session_scope() as session:
            session.execute(delete(Space).where(Space.id == space_id))
            session.execute(delete(User).where(User.id == space_id))
        redis = get_redis()
        for key in redis.scan_iter(match=KEYS.prefix + "*" + space_id + "*"):
            redis.delete(key)


if __name__ == "__main__":
    main()
