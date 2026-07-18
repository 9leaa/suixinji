# Example Memory Write Trace

```text
trace_type: memory_write
space_id: p_example
note_id: note_example

note_saved
memory_extraction_started
candidate_extracted
retrieval_started
candidate_memories_found
relation_decided
evolution_started
memory_superseded
vector_written
trace_finished
```

This example intentionally shows only IDs, counts, step names, statuses, decision confidence, and reason summaries. It does not include the full original message, prompt text, API keys, or a full user profile. The final trace is stored in both SQLite and the JSONL operations copy.
