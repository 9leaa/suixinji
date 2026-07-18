# Memory Quality Baseline

`baseline.json` is the original Stage 0 deterministic baseline. The corrected
relation labels introduced during Stage 1 are evaluated with the same dataset
and evaluator in these directly comparable reports:

- `baseline_v2.json`: commit `deda2b3`, before Stage 1 implementation;
- `stage1.json`: Stage 1 working tree on `optimize/stage1-memory-correctness`.
- `stage2.json`: Stage 2 performance working tree; correctness gates remain
  comparable with Stage 1 after repository query-path changes.

All reports use the rules extractor and adjudicator without GPT or embedding
calls, so model latency, cost, and API noise do not affect the comparison.

`eval/memory/quality_cases.jsonl` contains 360 labeled cases:

- 120 candidate extraction cases;
- 120 relation adjudication cases covering all six relations;
- 60 retrieval cases;
- 60 end-to-end write/query cases, including preference negation and state change.

The report records extraction F1 by memory type, relation Macro-F1 and
destructive false-positive rate, Recall@20/MRR, and end-to-end accuracy. A
quality improvement must keep the dataset and metric definitions fixed, then
write a new report beside this baseline.

The Stage 1 comparison and acceptance decision are recorded in
`docs/metrics/stage1_memory_correctness.json` and
`docs/stage1_memory_correctness_report.md`.
