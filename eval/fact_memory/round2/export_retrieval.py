"""Read-only export of hybrid retrieval telemetry without credential material."""

import json
import sys
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run as runner
from core.config import get_embedding_config
from cases import OUT

run_id = sys.argv[1]
data = json.loads((OUT / f"results-{run_id}.json").read_text())
cfg = get_embedding_config()
rows = [{"id": r["id"], "space": r["space"], "retrieval_runs": runner.fm.items(r["space"], "retrieval")} for r in data["results"]]
retrieval_file = runner.ROOT / "memory/fact_retrieval.py"
metadata = {
    "read_only_auxiliary_export": True,
    "run_id": run_id,
    "embedding": {"model": cfg.model, "dimension": cfg.dimension, "endpoint_sha256": hashlib.sha256(cfg.base_url.encode()).hexdigest()},
    "fact_retrieval_current_disk_sha256": hashlib.sha256(retrieval_file.read_bytes()).hexdigest() if retrieval_file.exists() else None,
    "note": "检索分数与IDs来自真实fm_retrieval；源码hash为导出时磁盘值，最终完整运行会在启动前元数据中冻结。",
}
runner.dump(OUT / f"hybrid-retrieval-{run_id}.json", {"metadata": metadata, "results": rows})
print(
    json.dumps(
        {
            "cases": len(rows),
            "retrieval_runs": sum(len(r["retrieval_runs"]) for r in rows),
            "degraded_runs": sum(bool(t.get("degraded")) for r in rows for t in r["retrieval_runs"]),
        }
    )
)
