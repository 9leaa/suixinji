"""Shared explicit commands for the console and Feishu; no preference mutations."""
import json
import re
from core import settings
from memory import fact_memory as fm


def natural_command(text):
    """Only an explicit memory-management imperative opens an approval preview."""
    if not settings.FACT_MEMORY_ENABLED:
        return None
    match = re.fullmatch(r"(?:请|帮我|请帮我)?(?:删除|忘掉|清除)(?:关于|有关)?(.+?)(?:相关的|相关|的)?记忆[。！! ]*", text.strip())
    if match:
        return "/memory facts forget " + match.group(1).strip()
    return None


def execute(space, text):
    if not settings.FACT_MEMORY_ENABLED or not text.startswith("/memory facts"):
        return None
    tail = text[len("/memory facts"):].strip()
    parts = tail.split(maxsplit=2)
    op = parts[0] if parts else "list"
    if op == "consolidate":
        kind = parts[1] if len(parts) > 1 else None
        return fm.manual_run(space, kind)["message"]
    if op == "list":
        return json.dumps(fm.cards(space), ensure_ascii=False, indent=2)
    if op == "questions":
        return json.dumps([q for q in fm.items(space, "question") if q["status"] == "open"], ensure_ascii=False, indent=2)
    if op == "answer" and len(parts) == 3:
        fm.answer_question(space, parts[1], parts[2])
        return "已保存补充说明。可立即 /memory facts consolidate 查看整理及剩余疑问。"
    if op == "forget" and len(parts) > 1:
        result = fm.request_forget(space, tail[len("forget"):].strip())
        if not result["candidate_ids"]:
            return "没有找到这个主题的有效记忆，未删除任何内容。"
        return json.dumps(result, ensure_ascii=False, indent=2) + "\n请打开上方“记忆整理与审批”，在删除申请中逐条或全选审批。未审批不会删除；也可执行 /memory facts approve-delete <申请ID> <ID1,ID2>。"
    if op == "approve-delete" and len(parts) == 3:
        return json.dumps(fm.approve_forget(space, parts[1], parts[2].split(",")), ensure_ascii=False, indent=2)
    return "用法：/memory facts list｜consolidate [semantic|episodic]｜questions｜answer <问题ID> <说明>｜forget <主题>｜approve-delete <申请ID> <ID1,ID2>"
