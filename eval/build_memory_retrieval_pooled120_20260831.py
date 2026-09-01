"""Build a deterministic Layer3 retrieval subset with non-conflicting distractors."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.layer3.run_layer3_eval import DATA_FILES, load_cases_with_manifest, preflight_cases


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity(raw: dict[str, Any]) -> tuple[str, str]:
    topic = raw.get("canonical_topic") or raw.get("attribute") or raw.get("entity") or raw.get("content")
    return str(raw.get("memory_type") or ""), str(topic or "").strip().casefold()


def _signature(raw: dict[str, Any]) -> str:
    keys = (
        "memory_type", "status", "task_status", "content", "canonical_topic",
        "entity", "attribute", "current_value", "polarity", "updated_at",
    )
    return json.dumps({key: raw.get(key) for key in keys}, ensure_ascii=False, sort_keys=True)


def _select_cases(cases: list[dict[str, Any]], per_dataset: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[str(case.get("dataset") or "")].append(case)
    selected: list[dict[str, Any]] = []
    for dataset in sorted(grouped):
        ranked = sorted(grouped[dataset], key=lambda item: _hash(str(item.get("case_id") or "")))
        selected.extend(ranked[:per_dataset])
    return selected


def build(cases: list[dict[str, Any]], *, per_dataset: int, distractors: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = _select_cases(cases, per_dataset)
    catalog: dict[str, dict[str, Any]] = {}
    for case in cases:
        snapshot = (case.get("input") or {}).get("memory_snapshot") or {}
        for raw in snapshot.get("memories") or []:
            if str(raw.get("status") or "active") != "active":
                continue
            if str(raw.get("sensitivity") or "normal") != "normal":
                continue
            catalog.setdefault(_signature(raw), raw)
    output: list[dict[str, Any]] = []
    actual_counts: list[int] = []
    for source in selected:
        case = copy.deepcopy(source)
        case_id = str(case["case_id"])
        snapshot = case["input"]["memory_snapshot"]
        existing_identities = {_identity(raw) for raw in snapshot.get("memories") or []}
        existing_content = {str(raw.get("content") or "").strip().casefold() for raw in snapshot.get("memories") or []}
        candidates = [
            raw for signature, raw in sorted(catalog.items(), key=lambda pair: _hash(case_id + pair[0]))
            if _identity(raw) not in existing_identities
            and str(raw.get("content") or "").strip().casefold() not in existing_content
        ][:distractors]
        for index, raw in enumerate(candidates, 1):
            memory = copy.deepcopy(raw)
            memory_ref = f"d{index:03d}"
            source_ref = f"ds{index:03d}"
            memory["memory_ref"] = memory_ref
            memory["source_refs"] = [source_ref]
            memory["status"] = "active"
            memory["sensitivity"] = "normal"
            memory["access_scope"] = "owner"
            snapshot.setdefault("memories", []).append(memory)
            snapshot.setdefault("sources", []).append({
                "source_ref": source_ref,
                "note_id": f"note_{case_id}_{source_ref}",
                "evidence_text": str(memory.get("content") or ""),
                "observed_at": memory.get("updated_at") or case["input"].get("query_time"),
                "authority": "user",
            })
        actual_counts.append(len(candidates))
        case["case_id"] = f"pooled_{case_id}"
        case.setdefault("coverage_tags", []).extend(["pooled_distractors", f"distractors_{len(candidates)}"])
        output.append(case)
    preflight = preflight_cases(output)
    type_counts: Counter[str] = Counter()
    answer_counts: Counter[str] = Counter()
    for case in output:
        answer_counts[str(case["expected"]["answer_type"])] += 1
        relevant = set(case["expected"].get("relevant_current_refs") or [])
        for raw in case["input"]["memory_snapshot"].get("memories") or []:
            if str(raw.get("memory_ref")) in relevant:
                type_counts[str(raw.get("memory_type") or "")] += 1
    manifest = {
        "version": "memory-retrieval-pooled120-v1",
        "source_cases": len(cases),
        "selected_cases": len(output),
        "per_dataset": per_dataset,
        "requested_distractors_per_case": distractors,
        "min_actual_distractors": min(actual_counts, default=0),
        "max_actual_distractors": max(actual_counts, default=0),
        "mean_actual_distractors": sum(actual_counts) / len(actual_counts) if actual_counts else 0.0,
        "dataset_counts": dict(Counter(str(case.get("dataset") or "") for case in output)),
        "answer_type_counts": dict(answer_counts),
        "gold_memory_type_counts": dict(type_counts),
        "selection": "sha256(case_id), deterministic",
        "distractor_rule": "active normal memories with different identity and content",
        "preflight": preflight,
    }
    return output, manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(ROOT / "eval" / "layer3" / "data_v2"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--per-dataset", type=int, default=24)
    parser.add_argument("--distractors", type=int, default=30)
    args = parser.parse_args()
    cases, source_manifest = load_cases_with_manifest(args.data_dir)
    output, manifest = build(cases, per_dataset=args.per_dataset, distractors=args.distractors)
    target = Path(args.output_dir)
    target.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in output:
        grouped[str(case["dataset"])].append(case)
    dataset_to_file = {
        "current_state_retrieval": DATA_FILES[0],
        "history_and_temporal": DATA_FILES[1],
        "multi_memory_answer_and_citation": DATA_FILES[2],
        "no_answer_conflict_and_stale": DATA_FILES[3],
        "semantic_paraphrase_and_noise": DATA_FILES[4],
    }
    for dataset, filename in dataset_to_file.items():
        with (target / filename).open("w", encoding="utf-8") as handle:
            for case in grouped.get(dataset, []):
                handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    payload = {"source": source_manifest, "derived": manifest}
    (target / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
