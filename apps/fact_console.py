"""Authenticated review UI/API for semantic/event consolidation."""
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from apps.console import authorize
from core import settings
from memory import fact_memory as fm

router = APIRouter()


def check_space(space_id, tenant):
    if not settings.FACT_MEMORY_ENABLED:
        raise HTTPException(404, "fact memory disabled")
    from sqlalchemy import select
    from infrastructure.database import session_scope
    from infrastructure.schema import Space
    with session_scope() as session:
        row = session.scalar(select(Space).where(Space.id == space_id, Space.tenant_id == tenant))
        if row is None:
            raise HTTPException(404, "space not found")


class Operation(BaseModel):
    action: str
    memory_type: str | None = None
    id: str | None = None
    content: str | None = None
    decision: str | None = None
    expected_version: int | None = None
    selected_ids: list[str] = Field(default_factory=list)


@router.get("/fact-memory", include_in_schema=False)
def page():
    if not settings.FACT_MEMORY_ENABLED:
        raise HTTPException(404)
    return FileResponse(Path(__file__).parent / "console_static" / "fact-memory.html")


@router.get("/api/fact-memory/{space_id}")
def state(space_id: str, tenant=Depends(authorize)):
    check_space(space_id, tenant)
    return {"cards": fm.cards(space_id), "facts": fm.active_facts(space_id),
            "questions": [q for q in fm.items(space_id, "question") if q["status"] == "open"],
            "runs": sorted(fm.items(space_id, "run"), key=lambda r: r["started_at"], reverse=True)[:20],
            "deletions": [r for r in fm.items(space_id, "delete")
                          if r["status"] != "completed" and r["candidate_ids"]],
            "schedule": fm.items(space_id, "schedule"), "due": fm.due(space_id)}


@router.post("/api/fact-memory/{space_id}")
def operate(space_id: str, request: Operation, tenant=Depends(authorize)):
    check_space(space_id, tenant)
    try:
        if request.action == "consolidate":
            return fm.manual_run(space_id, request.memory_type)
        if request.action == "answer":
            return fm.answer_question(space_id, request.id, request.content or "")
        if request.action == "review":
            return fm.review_memory(space_id, request.id, decision=request.decision,
                                    content=request.content, expected_version=request.expected_version)
        if request.action == "preview_delete":
            return fm.request_forget(space_id, request.content or "")
        if request.action == "approve_delete":
            return fm.approve_forget(space_id, request.id, request.selected_ids)
        if request.action == "history":
            return {"versions": [r for r in fm.items(space_id, "version") if r["id"] == request.id]}
        raise ValueError("unknown action")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
