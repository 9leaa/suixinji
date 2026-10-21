import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run as runner
from cases import OUT

run_id = sys.argv[1]
data = json.loads((OUT / f"results-{run_id}.json").read_text())
rows = []
for r in data["results"]:
    for m in r["evidence"]["messages"]:
        n = m["note"]
        saved = runner.find_note(r["space"], n["id"])
        rows.append({"case_id": r["id"], "note_id": n["id"], "preserved": bool(saved and saved["text"] == n["text"])})
result = {
    "run_id": run_id,
    "read_only_db_verification": True,
    "all_preserved": all(x["preserved"] for x in rows),
    "total": len(rows),
    "notes": rows,
}
runner.dump(OUT / f"notes-preserved-{run_id}.json", result)
assert result["all_preserved"]
print(json.dumps({k: v for k, v in result.items() if k != "notes"}))
