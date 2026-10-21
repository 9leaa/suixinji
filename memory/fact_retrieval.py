"""Shared lexical + semantic recall for extraction, consolidation and queries."""
from concurrent.futures import ThreadPoolExecutor
import math

from core.lifecycle import traced, annotate
from core import settings
from core.config import get_embedding_config
from core.llm_client import embed_text


@traced('记忆 · 补充检索与评分', 'retrieval')
def rank(space, query, rows, limit, *, stage='补充检索'):
    from memory import fact_memory as fm
    ranked = [{**row, "lexical_score": fm.score(query, row["content"] + " " + " ".join(row.get("entities", [])))}
              for row in rows]
    for row in ranked:
        row["score"] = row["lexical_score"]
        row["retrieval_channels"] = ["lexical"]
    degraded = None
    if ranked and query.strip() and settings.FACT_MEMORY_SEMANTIC_SEARCH_ENABLED:
        config = get_embedding_config()

        def vector(text):
            ident = fm.key(config.base_url, config.model, config.dimension, text)
            cached = fm.get(space, "embedding", ident)
            if cached:
                return cached["vector"]
            value = embed_text(text)
            fm.store.put(space, "fm_embedding", ident, {"id": ident, "vector": value})
            return value

        try:
            qv = vector(query)
            qn = math.sqrt(sum(v * v for v in qv))
            with ThreadPoolExecutor(max_workers=6) as pool:
                vectors = list(pool.map(vector, [row["content"] for row in ranked]))
            for row, values in zip(ranked, vectors):
                vn = math.sqrt(sum(v * v for v in values))
                cosine = sum(a * b for a, b in zip(qv, values)) / max(qn * vn, 1e-12)
                row["semantic_score"] = cosine
                row["score"] = .65 * max(cosine, 0) + .35 * row["lexical_score"]
                row["retrieval_channels"].append("semantic")
        except RuntimeError as error:
            # Preserve lexical service during a provider outage and expose the degradation.
            degraded = type(error).__name__
    ranked.sort(key=lambda r: (r["score"], str(r.get("mentioned_at") or r.get("updated_at", ""))), reverse=True)
    eligible = [r for r in ranked if r["lexical_score"] > 0 or r.get("semantic_score", 0) >= .4]
    result = eligible[:limit]
    decisions = []
    for index, row in enumerate(ranked):
        eligible_row = row["lexical_score"] > 0 or row.get("semantic_score", 0) >= .4
        reason = ("词法命中" if row["lexical_score"] > 0 else "向量相似度达到0.4") if eligible_row else "未满足候选保留条件"
        if eligible_row and row not in result:
            reason = "通过候选过滤，因数量限制截断"
        decisions.append({**{k: row.get(k) for k in ("id", "lexical_score", "semantic_score", "score")},
                          "rank": index + 1, "retained": row in result, "reason": reason})
    annotate(stage=stage, input_count=len(rows), eligible_count=len(eligible), output_count=len(result),
             score_formula="0.65 × 向量相似度（最低0） + 0.35 × 词法分；无向量时使用词法分",
             filter_rule="词法分 > 0 或向量相似度 ≥ 0.4；不使用旧分支 min_score",
             decisions=decisions, degraded=degraded)
    fm.store.put(space, "fm_retrieval", fm.key(query, fm.now_iso()), {
        "query": query, "created_at": fm.now_iso(), "result_ids": [r["id"] for r in result],
        "scores": [{k: r.get(k) for k in ("id", "lexical_score", "semantic_score", "score")} for r in result],
        "degraded": degraded,
    })
    return result
