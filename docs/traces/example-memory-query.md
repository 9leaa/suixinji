# Example Memory Query Trace

```text
trace_type: memory_query
space_id: p_example
query_len: 12

query_received
query_routed
memory_search
note_search
rerank
evidence_selected
answer_generated
answer_returned
trace_finished
```

The query trace is designed to explain why a memory was used without storing the complete user query or full prompt by default.

