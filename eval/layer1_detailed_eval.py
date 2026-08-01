from __future__ import annotations
import argparse
import json
import threading
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import httpx
from openai import OpenAI
from core import llm_client
from memory import extractor
from memory.models import memory_key_for
from memory.canonicalizer import preference_key, semantic_key, task_key
from memory.policies.preference import preference_polarity

FIELDS = [
    "entity",
    "attribute",
    "operation",
    "canonical_topic",
    "task_status",
    "old_value",
    "new_value",
    "valid_from",
    "valid_until",
    "polarity",
    "memory_key",
]
KEY_FIELDS = FIELDS[:7]
TYPES = ["preference", "task", "semantic", "episodic"]
_tls = threading.local()


def safe(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [safe(x) for x in v]
    return str(v)


def cand(c):
    s = dict(getattr(c, "scope", {}) or {})
    return {
        "memory_type": getattr(c, "memory_type", None),
        "entity": getattr(c, "subject", None),
        "attribute": getattr(c, "predicate", None),
        "operation": s.get("operation"),
        "canonical_topic": s.get("canonical_topic"),
        "task_status": getattr(c, "task_status", None),
        "old_value": s.get("old_value"),
        "new_value": s.get("new_value"),
        "valid_from": getattr(c, "valid_from", None),
        "valid_until": getattr(c, "valid_until", None),
        "polarity": getattr(c, "polarity", None),
        "memory_key": getattr(c, "effective_memory_key", None),
        "content": getattr(c, "content", None),
        "evidence_span": getattr(c, "evidence_span", None),
        "scope": s,
    }


def expected_key(row):
    e = row["expected_output"]["candidates"][0]
    typ = e["memory_type"]
    ent = e.get("entity") or "用户"
    attr = e.get("attribute") or typ
    topic = e.get("canonical_topic") or e.get("new_value") or ""
    op = e.get("operation") or "维护"
    if typ == "task":
        return task_key(ent, attr, op, "global")
    if typ == "semantic":
        return semantic_key(ent, attr, topic, "current")
    if typ == "preference":
        return preference_key(ent, topic, "global")
    return memory_key_for(
        typ, subject=ent, predicate=attr, object_value=e.get("new_value") or attr, content=e.get("content") or row["input"]["text"]
    )


def gold(row):
    e = dict(row["expected_output"]["candidates"][0])
    typ = e["memory_type"]
    e["polarity"] = preference_polarity(row["input"]["text"]) if typ == "preference" else None
    e["memory_key"] = expected_key(row)
    return {f: e.get(f) for f in ["memory_type", *FIELDS]}


def eq(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return str(a).strip() == str(b).strip()


def match(golds, preds):
    rem = list(preds)
    pairs = []
    for g in golds:
        p = next((x for x in rem if x.get("memory_type") == g.get("memory_type")), None)
        if p is None and rem:
            p = rem[0]
        if p is not None:
            rem.remove(p)
        pairs.append((g, p))
    return pairs, rem


def f1(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    return 2 * p * r / (p + r) if p + r else 0


def score(rows, key):
    tp = fp = fn = 0
    gt = Counter()
    pt = Counter()
    for r in rows:
        a = len(r["expected"])
        b = len(r[key])
        tp += min(a, b)
        fp += max(0, b - a)
        fn += max(0, a - b)
        gt.update(x["memory_type"] for x in r["expected"])
        pt.update(x.get("memory_type") for x in r[key])
    tf = {t: f1(min(gt[t], pt[t]), max(0, pt[t] - gt[t]), max(0, gt[t] - pt[t])) for t in TYPES}
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "candidate_f1": f1(tp, fp, fn),
        "memory_type_macro_f1": sum(tf.values()) / 4,
        "memory_type_f1": tf,
    }


def fields(rows, key):
    ok = Counter()
    total = Counter()
    by = defaultdict(lambda: Counter())
    byok = defaultdict(lambda: Counter())
    exact = 0
    n = 0
    for r in rows:
        for g, p in match(r["expected"], r[key])[0]:
            n += 1
            one = True
            t = g["memory_type"]
            for f in FIELDS:
                total[f] += 1
                by[t][f] += 1
                good = p is not None and eq(g.get(f), p.get(f))
                if good:
                    ok[f] += 1
                    byok[t][f] += 1
                else:
                    one = False
            exact += one
    acc = {f: ok[f] / total[f] if total[f] else 0 for f in FIELDS}
    return {
        "accuracy": acc,
        "correct": dict(ok),
        "total": dict(total),
        "exact": exact,
        "compared": n,
        "all_exact_rate": exact / n if n else 0,
        "by_type": {t: {f: byok[t][f] / by[t][f] if by[t][f] else 0 for f in FIELDS} for t in TYPES if by[t]},
    }


def confusion(rows, key):
    labels = TYPES + ["none"]
    m = {g: {p: 0 for p in labels} for g in labels}
    for r in rows:
        pairs, extra = match(r["expected"], r[key])
        for g, p in pairs:
            m[g["memory_type"]][p["memory_type"] if p else "none"] += 1
        for p in extra:
            m["none"][p.get("memory_type") or "none"] += 1
        if not r["expected"] and not extra:
            m["none"]["none"] += 1
    return m


def mdtable(headers, rows):
    return (
        "| "
        + " | ".join(headers)
        + " |\n|"
        + "|".join("---" for _ in headers)
        + "|\n"
        + "\n".join("| " + " | ".join(map(str, r)) + " |" for r in rows)
    )


def pct(x):
    return f"{x * 100:.2f}%"


def capture(*a, **kw):
    d = _ORIG(*a, **kw)
    _tls.raw = d
    return d


def client(config=None):
    config = config or llm_client.get_chat_config()
    kw = {"http_client": _HTTP, "timeout": max(60, float(getattr(config, "timeout_seconds", 15) or 15)), "max_retries": 0}
    if getattr(config, "api_key", None):
        kw["api_key"] = config.api_key
    if getattr(config, "base_url", None):
        kw["base_url"] = config.base_url
    return OpenAI(**kw)


def runone(row, max_attempts):
    last = None
    for attempt in range(1, max_attempts + 1):
        _tls.raw = None
        try:
            cs = extractor.extract_llm_candidates(
                row["case_id"],
                row["input"]["text"],
                row["input"].get("classification"),
                hints=extractor._rule_hints(row["case_id"], row["input"]["text"], row["input"].get("classification")),
            )
            return {"pred": [cand(c) for c in cs], "raw": safe(getattr(_tls, "raw", None)), "attempts": attempt, "error": None}
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if attempt < max_attempts:
                time.sleep(min(2, 0.2 * attempt))
    return {"pred": [], "raw": safe(getattr(_tls, "raw", None)), "attempts": max_attempts, "error": last}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="key_fields_and_status.jsonl")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-attempts", type=int, default=20)
    a = ap.parse_args()
    root = Path("/home/zcj/suixinji")
    z = zipfile.ZipFile(root / "suixinji_layer1_repaired_datasets.zip")
    rows = [json.loads(x) for x in z.read(a.dataset).decode().splitlines() if x.strip()]
    for r in rows:
        r["expected"] = [gold(r)]
    out = root / "eval/results" / ("layer1_detailed_" + datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
    out.mkdir(parents=True)
    rules = []
    for r in rows:
        rules.append(
            [cand(c) for c in extractor.extract_rule_candidates(r["case_id"], r["input"]["text"], r["input"].get("classification"))]
        )
    global _HTTP, _ORIG
    _HTTP = httpx.Client(
        verify=False,
        follow_redirects=True,
        timeout=60,
        limits=httpx.Limits(max_connections=max(16, a.workers * 2), max_keepalive_connections=max(8, a.workers)),
    )
    _ORIG = extractor.complete_json
    extractor.complete_json = capture
    llm_client.build_openai_client = client
    results = [None] * len(rows)
    done = 0
    retries = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        fs = {pool.submit(runone, r, a.max_attempts): i for i, r in enumerate(rows)}
        for f in as_completed(fs):
            i = fs[f]
            results[i] = f.result()
            done += 1
            retries += max(0, results[i]["attempts"] - 1)
            if done % 20 == 0 or done == len(rows):
                print(f"progress {done}/{len(rows)} retries={retries}", flush=True)
    _HTTP.close()
    data = []
    for r, ru, ll in zip(rows, rules, results):
        data.append(
            {
                "case_id": r["case_id"],
                "difficulty": r.get("difficulty"),
                "coverage_tags": r.get("coverage_tags", []),
                "text": r["input"]["text"],
                "expected": r["expected"],
                "rules": ru,
                "llm": ll["pred"],
                "raw_llm_output": ll.get("raw"),
                "llm_attempts": ll["attempts"],
                "llm_error": ll.get("error"),
            }
        )
    for f in FIELDS:
        with (out / f"failed_{f}.jsonl").open("w") as h:
            for r in data:
                for g, p in match(r["expected"], r["llm"])[0]:
                    if p is None or not eq(g.get(f), p.get(f)):
                        h.write(
                            json.dumps(
                                {
                                    "case_id": r["case_id"],
                                    "text": r["text"],
                                    "gold": g.get(f),
                                    "pred": None if p is None else p.get(f),
                                    "gold_memory_type": g.get("memory_type"),
                                    "pred_memory_type": None if p is None else p.get("memory_type"),
                                    "raw_llm_output": r["raw_llm_output"],
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
    for r in data:
        r["source"] = "llm_success" if not r["llm_error"] else "llm_failed"
    ls = score(data, "llm")
    rs = score(data, "rules")
    lf = fields(data, "llm")
    rf = fields(data, "rules")
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "dataset": a.dataset,
        "cases": len(data),
        "llm_success": sum(not r["llm_error"] for r in data),
        "llm_failures": sum(bool(r["llm_error"]) for r in data),
        "transport_retries": sum(max(0, r["llm_attempts"] - 1) for r in data),
        "llm": {"candidate": ls, "fields": lf, "confusion": confusion(data, "llm")},
        "rules": {"candidate": rs, "fields": rf, "confusion": confusion(data, "rules")},
        "source_split": {
            "LLM Candidate": len(data),
            "Rules Candidate": len(data),
            "Hybrid LLM success": sum(not r["llm_error"] for r in data),
            "Hybrid rules fallback": 0,
        },
    }
    (out / "rows.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in data) + "\n")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    cm = summary["llm"]["confusion"]
    lines = [
        "# Layer 1 结构化字段详细评测（" + a.dataset + "）",
        "",
        "只评测，不写入 memory；保存 LLM 原始 JSON、规则候选、规范化候选和失败样本。",
        f"- 样本：{len(data)}；LLM 成功：{summary['llm_success']}；LLM 失败：{summary['llm_failures']}；传输重试：{summary['transport_retries']}",
        "- Hybrid 规则兜底：0（严格模式）",
        "",
        "## 1. 来源对比",
        "",
        mdtable(
            ["来源", "Candidate F1", "Memory Type Macro-F1", "Key-field Accuracy", "All-fields Exact"],
            [
                [
                    "LLM Candidate",
                    pct(ls["candidate_f1"]),
                    pct(ls["memory_type_macro_f1"]),
                    pct(sum(lf["accuracy"][f] for f in KEY_FIELDS) / len(KEY_FIELDS)),
                    pct(lf["all_exact_rate"]),
                ],
                [
                    "Rules Candidate",
                    pct(rs["candidate_f1"]),
                    pct(rs["memory_type_macro_f1"]),
                    pct(sum(rf["accuracy"][f] for f in KEY_FIELDS) / len(KEY_FIELDS)),
                    pct(rf["all_exact_rate"]),
                ],
            ],
        ),
        "",
        "## 2. LLM Memory 类型混淆矩阵",
        "",
        "行=Gold，列=预测；none=漏抽取或额外候选。",
        "",
        mdtable(["Gold \\ Pred"] + TYPES + ["none"], [[g] + [cm[g][p] for p in TYPES + ["none"]] for g in TYPES + ["none"]]),
        "",
        "## 3. LLM 逐字段准确率",
        "",
        mdtable(
            ["字段", "正确", "总数", "Accuracy"],
            [[f, lf["correct"].get(f, 0), lf["total"].get(f, 0), pct(lf["accuracy"][f])] for f in FIELDS],
        ),
        "",
        f"全部 11 字段同时正确：{lf['exact']}/{lf['compared']}（{pct(lf['all_exact_rate'])}）。",
        "",
        "## 4. 按 Memory 类型拆分字段准确率（LLM）",
        "",
        mdtable(
            ["Memory 类型"] + FIELDS, [[t] + [pct(lf["by_type"].get(t, {}).get(f, 0)) for f in FIELDS] for t in TYPES if t in lf["by_type"]]
        ),
        "",
        "## 5. 抽取来源统计",
        "",
        mdtable(
            ["来源", "数量", "说明"],
            [
                ["LLM Candidate", len(data), "LLM 结构化抽取"],
                ["Rules Candidate", len(data), "同批样本规则抽取"],
                ["Hybrid 中 LLM 成功", summary["llm_success"], "实际采用 LLM"],
                ["Hybrid 中规则兜底", 0, "严格模式禁用 fallback"],
            ],
        ),
        "",
        "## 6. 失败样本",
        "",
        mdtable(["字段", "文件", "失败数"], [[f, f"failed_{f}.jsonl", sum(1 for _ in (out / f"failed_{f}.jsonl").open())] for f in FIELDS]),
        "",
        "## 7. 结论",
        "",
        "若 Candidate F1 高而字段准确率低，说明主要瓶颈是字段归一化、canonical_topic、old/new value 或 task_status，而不是候选召回。",
    ]
    (out / "report.md").write_text("\n".join(lines))
    print(json.dumps({"out_dir": str(out), "report": str(out / "report.md"), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
