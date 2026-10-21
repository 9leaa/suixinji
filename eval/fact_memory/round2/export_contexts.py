"""Read-only export of exact persisted extraction context IDs, with per-step records."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run as runner
from cases import OUT

run_id = sys.argv[1]
results = json.loads((OUT / f"results-{run_id}.json").read_text())["results"]
export = []
for row in results:
    context_by_note = {r["id"]: r for r in runner.fm.items(row["space"], "extraction_context")}
    traces = []
    for i, msg in enumerate(row["evidence"]["messages"]):
        note_id = msg["note"]["id"]
        ctx = context_by_note.get(note_id)
        snap = row["evidence"]["snapshots"][i]
        pool = {r["id"]: r for kind in ["fact", "card"] for r in snap[kind]}
        traces.append(
            {
                "note_id": note_id,
                "message": msg["note"]["text"],
                "context": ctx,
                "resolved_context_records": [pool[x] for x in (ctx or {}).get("result_ids", []) if x in pool],
            }
        )
    export.append({"id": row["id"], "space": row["space"], "context_traces": traces})
runner.dump(
    OUT / f"extraction-contexts-{run_id}.json",
    {
        "run_id": run_id,
        "read_only_auxiliary_export": True,
        "note": "IDs来自实际抽取时持久化的fm_extraction_context，正文由同一次逐条snapshot映射；不是全量数据库补召回。",
        "results": export,
    },
)
print(
    json.dumps(
        {
            "run_id": run_id,
            "cases": len(export),
            "contexts": sum(len(x["context_traces"]) for x in export),
            "missing_contexts": sum(t["context"] is None for x in export for t in x["context_traces"]),
        }
    )
)
