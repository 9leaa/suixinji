# Layer 2 consolidation evaluation

This evaluator starts from validated `MemoryCandidate` JSONL rows and calls
the real `memory.consolidator.consolidate_candidate` chain. It never invokes
the Stage 1 extractor and each case uses an isolated temporary SQLite file.

```bash
python eval/layer2/validate_dataset.py --data-dir eval/layer2/data
python eval/layer2/run_consolidation_eval.py \
  --data-dir eval/layer2/data \
  --output-dir eval/results/layer2
```

The output contains the run manifest, normalized predictions, failed cases,
relation/action confusion matrices, version/source/orphan reports, and a
Markdown summary.
