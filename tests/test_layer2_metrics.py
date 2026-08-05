from eval.layer2.metrics import _binary_prf, _case_final_exact

def test_pending_metrics_are_positive_class_metrics():
    result = _binary_prf([True, True, False, False], [True, False, True, False])
    assert result["tp"] == 1 and result["fp"] == 1 and result["fn"] == 1 and result["tn"] == 1
    assert result["precision"] == 0.5 and result["recall"] == 0.5 and result["f1"] == 0.5

def test_terminal_task_reference_is_retained_in_exact_state():
    case = {"gold": {"expected_active_memory_refs": ["m1"], "duplicate_active_count": 0, "stale_active_count": 0, "final_memories": [{"memory_ref": "m1", "memory_type": "task", "task_status": "done", "status": "active", "version_sequence": 5, "source_note_ids": ["n1"]}]}, "predicted_state": {"expected_active_memory_refs": [], "duplicate_active_count": 0, "stale_active_count": 0, "active_memories": [], "all_memories": [{"memory_ref": "m1", "memory_type": "task", "task_status": "done", "status": "archived", "version_sequence": 6, "source_note_ids": ["n1", "n2"]}]}}
    assert _case_final_exact(case)
