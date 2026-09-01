from agent.ask_models import AskPlan, QueryUnit, UnitEvidenceBundle, UnitResolution
from agent.ask_executor import AskExecutionResult
from agent.ask_plan_validator import safe_fallback_plan, validate_ask_plan


def _unit(unit_id: str, question: str, intent: str, span: str, **kwargs):
    return {
        "id": unit_id,
        "question": question,
        "source_spans": [span],
        "intent": intent,
        "memory_type": kwargs.pop("memory_type", None),
        "time_mode": kwargs.pop("time_mode", "current"),
        "evidence_mode": kwargs.pop("evidence_mode", "current_state"),
        **kwargs,
    }


def test_validator_keeps_single_sentence_multiple_units():
    question = "我现在住哪又喜欢喝什么？"
    plan = validate_ask_plan(
        {
            "original_query": question,
            "answer_mode": "direct",
            "units": [
                _unit("u1", "我现在住在哪里？", "semantic_current", "我现在住哪", facet="location"),
                _unit("u2", "我喜欢喝什么？", "preference_current", "喜欢喝什么"),
            ],
        },
        question=question,
    )

    assert [unit.intent for unit in plan.units] == ["semantic_current", "preference_current"]
    assert [unit.memory_type for unit in plan.units] == ["semantic", "preference"]


def test_validator_repairs_entity_losing_planner_rewrite():
    question = "\u6211\u6700\u559c\u6b22\u7684\u7535\u5f71\u662f\u4ec0\u4e48\uff1f"
    plan = validate_ask_plan(
        {
            "original_query": question,
            "units": [
                _unit(
                    "u1", "\u6211\u559c\u6b22\u5496\u5561\u5417\uff1f", "preference_current", question,
                    memory_type="preference", topic="\u5496\u5561",
                )
            ],
        },
        question=question,
    )
    assert plan.units[0].question == question
    assert plan.units[0].topic == question


def test_validator_keeps_background_without_creating_extra_unit():
    question = "我之前在北京住过，后来搬家了。我现在住哪？"
    plan = validate_ask_plan(
        {
            "original_query": question,
            "context": [{"text": "我之前在北京住过，后来搬家了", "source": "current_message"}],
            "units": [_unit("u1", "我现在住在哪里？", "semantic_current", "我现在住哪", facet="location")],
        },
        question=question,
    )

    assert len(plan.units) == 1
    assert plan.context[0].text.startswith("我之前")


def test_validator_rejects_unsupported_unit_and_dependency_cycle():
    question = "我住哪又喜欢喝什么？"
    plan = validate_ask_plan(
        {
            "original_query": question,
            "units": [
                _unit("u1", "我住哪？", "semantic_current", "我住哪", facet="location", depends_on=["u2"]),
                _unit("u2", "我喜欢喝什么？", "preference_current", "喜欢喝什么", depends_on=["u1"]),
            ],
        },
        question=question,
    )

    assert plan == safe_fallback_plan(question)


def test_executor_routes_each_unit_to_its_domain_tool(monkeypatch):
    from agent import ask_executor

    calls = []
    monkeypatch.setattr(
        ask_executor,
        "task_status_search",
        lambda *args, **kwargs: calls.append("task") or [{"id": "task-1", "memory_type": "task", "content": "简历待完成", "sources": []}],
    )
    monkeypatch.setattr(
        ask_executor,
        "memory_search",
        lambda _space, _query, **kwargs: calls.append(kwargs["memory_type"]) or [{
            "id": f"{kwargs['memory_type']}-1",
            "memory_type": kwargs["memory_type"],
            "content": "测试证据",
            "sources": [],
        }],
    )
    plan = AskPlan(
        original_query="简历做完了吗，我喜欢喝什么？",
        units=[
            QueryUnit(**_unit("u1", "简历做完了吗？", "task_state", "简历做完", memory_type="task")),
            QueryUnit(**_unit("u2", "我喜欢喝什么？", "preference_current", "喜欢喝什么", memory_type="preference")),
        ],
    )

    result = ask_executor.execute_ask_plan("space", plan)

    assert set(calls) == {"task", "preference"}
    assert [bundle.unit_id for bundle in result.bundles] == ["u1", "u2"]
    assert all(bundle.resolution.status == "resolved" for bundle in result.bundles)


def test_semantic_current_uses_projection_without_dropping_conflict_evidence(monkeypatch):
    from agent import ask_executor

    class Record:
        def __init__(self, value):
            self.predicate = "location"
            self.id = value["id"]
            self.value = value

        def to_dict(self):
            return self.value

    beijing = {"id": "beijing", "memory_type": "semantic", "content": "用户住在北京", "predicate": "location", "updated_at": "2026-08-01T00:00:00+00:00", "sources": []}
    shanghai = {"id": "shanghai", "memory_type": "semantic", "content": "用户已经搬到上海", "predicate": "location", "updated_at": "2026-08-02T00:00:00+00:00", "sources": []}
    monkeypatch.setattr(ask_executor, "memory_search", lambda *args, **kwargs: [beijing])
    monkeypatch.setattr(ask_executor, "list_memories", lambda *args, **kwargs: [Record(beijing), Record(shanghai)])
    from repositories.postgres import semantic_profile_projection as projection_repository
    monkeypatch.setattr(
        projection_repository,
        "get_semantic_profile_projection",
        lambda *args, **kwargs: {
            "projection": {"current_memory_ids": ["shanghai"], "uncertain_memory_ids": ["beijing"]},
        },
    )
    unit = QueryUnit(**_unit("u1", "我现在住在哪里？", "semantic_current", "住在哪里", memory_type="semantic", facet="location"))

    tool, records = ask_executor._execute_domain_tool("space", unit, access_context=None)
    bundle = ask_executor._resolve(unit, records, space_id="space", tool=tool)

    assert [item["id"] for item in records[:2]] == ["shanghai", "beijing"]
    assert bundle.resolution.value == "用户已经搬到上海"
    assert [item.evidence_id for item in bundle.evidence] == ["shanghai", "beijing"]


def test_semantic_current_falls_back_to_valid_from_and_keeps_history(monkeypatch):
    from agent import ask_executor
    from repositories.postgres import semantic_profile_projection as projection_repository

    history = {
        "id": "old-city", "memory_type": "semantic", "content": "用户过去住在上海",
        "subject": "用户", "predicate": "常住地", "valid_from": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-08-03T00:00:00+00:00", "scope": {"canonical_topic": "常住地"}, "sources": [],
    }
    current = {
        "id": "current-city", "memory_type": "semantic", "content": "用户现在住在北京",
        "subject": "用户", "predicate": "常住地", "valid_from": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-02T00:00:00+00:00", "scope": {"canonical_topic": "常住地"}, "sources": [],
    }
    monkeypatch.setattr(
        projection_repository,
        "get_semantic_profile_projection",
        lambda *args, **kwargs: None,
    )
    unit = QueryUnit(**_unit(
        "u1", "我现在住在哪里？", "semantic_current", "住在哪里",
        memory_type="semantic", facet="location",
    ))

    bundle = ask_executor._resolve(
        unit, [history, current], space_id="space", tool="resolve_semantic_facts",
    )

    assert bundle.resolution.value == "用户现在住在北京"
    assert [item.evidence_id for item in bundle.evidence] == ["current-city", "old-city"]
    assert [item.evidence_role for item in bundle.evidence] == ["current_candidate", "historical"]


def test_workflow_rejects_answer_citation_from_another_unit(monkeypatch):
    from agent import ask_workflow
    from agent.ask_models import EvidenceItem

    question = "我住哪又喜欢喝什么？"
    plan = AskPlan(
        original_query=question,
        units=[
            QueryUnit(**_unit("u1", "我住哪？", "semantic_current", "住哪", memory_type="semantic", facet="location")),
            QueryUnit(**_unit("u2", "我喜欢喝什么？", "preference_current", "喜欢喝什么", memory_type="preference")),
        ],
    )
    bundles = [
        UnitEvidenceBundle(
            unit_id="u1",
            evidence=[EvidenceItem(evidence_id="mem-city", source_kind="memory", memory_type="semantic", content="用户住在上海")],
            resolution=UnitResolution(status="resolved", value="用户住在上海", reason_code="test", selected_evidence_ids=["mem-city"]),
        ),
        UnitEvidenceBundle(
            unit_id="u2",
            evidence=[EvidenceItem(evidence_id="mem-drink", source_kind="memory", memory_type="preference", content="用户喜欢喝可乐")],
            resolution=UnitResolution(status="resolved", value="用户喜欢喝可乐", reason_code="test", selected_evidence_ids=["mem-drink"]),
        ),
    ]
    monkeypatch.setattr(ask_workflow, "complete_json", lambda **_: {
        "unit_answers": [
            {"unit_id": "u1", "answer": "你住在上海", "evidence_ids": ["mem-drink"]},
            {"unit_id": "u2", "answer": "你喜欢可乐", "evidence_ids": ["mem-drink"]},
        ],
        "final_answer": "错误混用引用",
    })

    answer = ask_workflow._answer_from_bundles(question, plan, bundles)

    assert answer == "你喜欢可乐"


def test_planner_keeps_multisentence_single_question(monkeypatch):
    from agent import ask_planner

    question = "我之前在北京住过，后来搬家了。我现在住哪？"
    monkeypatch.setattr(ask_planner, "complete_json", lambda **_: {
        "original_query": question,
        "context": [{"text": "我之前在北京住过，后来搬家了", "source": "current_message"}],
        "units": [_unit("u1", "我现在住在哪里？", "semantic_current", "我现在住哪", facet="location")],
        "answer_mode": "direct",
    })

    plan = ask_planner.plan_ask(question, max_units=4)

    assert len(plan.units) == 1
    assert plan.units[0].intent == "semantic_current"


def test_v2_workflow_executes_plan_without_react(monkeypatch):
    from agent import ask_workflow, query_agent
    from agent.ask_models import EvidenceItem

    question = "我喜欢喝什么？"
    plan = AskPlan(
        original_query=question,
        units=[QueryUnit(**_unit("u1", question, "preference_current", "喜欢喝什么", memory_type="preference"))],
    )
    bundle = UnitEvidenceBundle(
        unit_id="u1",
        evidence=[EvidenceItem(evidence_id="mem-drink", source_kind="memory", memory_type="preference", content="用户喜欢喝可乐")],
        resolution=UnitResolution(status="resolved", value="用户喜欢喝可乐", reason_code="test", selected_evidence_ids=["mem-drink"]),
    )
    monkeypatch.setattr(ask_workflow, "build_shadow_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(ask_workflow, "wait_for_memory_barrier", lambda *args, **kwargs: {"status": "ready", "waited_ms": 0})
    monkeypatch.setattr(query_agent, "provisional_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(ask_workflow, "execute_ask_plan", lambda *args, **kwargs: AskExecutionResult(
        bundles=[bundle],
        records_by_unit={"u1": [{"id": "mem-drink", "memory_type": "preference", "content": "用户喜欢喝可乐", "sources": []}]},
    ))
    monkeypatch.setattr(ask_workflow, "hydrate_selected_evidence", lambda *args, **kwargs: [])
    monkeypatch.setattr(ask_workflow, "complete_json", lambda **_: {
        "unit_answers": [{"unit_id": "u1", "answer": "你喜欢喝可乐。", "evidence_ids": ["mem-drink"]}],
        "final_answer": "你喜欢喝可乐。",
    })

    trace = {"steps": []}
    result = ask_workflow.answer_question_v2("space", question, trace=trace)

    assert result.answer == "你喜欢喝可乐。"
    assert result.answer_source == "ask_v2_plan_execute"
    steps = {item["step"]: item for item in trace["steps"]}
    assert steps["ask_plan_generated"]["duration_ms"] >= 0
    assert steps["ask_answer_generated"]["output_summary"]["timeout_seconds"] > 0
    assert steps["ask_finished"]["output_summary"]["tool_calls"] == 0


def test_v2_provisional_note_does_not_become_memory_state(monkeypatch):
    from agent import ask_workflow, query_agent

    question = "我刚才说的论文答辩怎么样？"
    plan = AskPlan(
        original_query=question,
        units=[QueryUnit(**_unit("u1", question, "task_state", "论文答辩", memory_type="task"))],
    )
    provisional = [{"id": "note-new", "text": "本科论文答辩刚完成，后台还在抽取。"}]
    monkeypatch.setattr(ask_workflow, "build_shadow_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(ask_workflow, "wait_for_memory_barrier", lambda *args, **kwargs: {"status": "timeout", "waited_ms": 1})
    monkeypatch.setattr(query_agent, "provisional_search", lambda *args, **kwargs: provisional)

    result = ask_workflow.answer_question_v2("space", question)

    assert result.answer_source == "ask_v2_provisional_read_after_write"
    assert result.selected_records == provisional
    assert result.bundles == []
    assert "后台完善分类" in result.answer


def test_executor_repairs_task_miss_with_historical_evidence(monkeypatch):
    from agent import ask_executor

    unit = QueryUnit(**_unit("u1", "论文答辩怎么样？", "task_state", "论文答辩", memory_type="task"))
    plan = AskPlan(original_query="论文答辩怎么样？", units=[unit])
    result = AskExecutionResult(
        bundles=[UnitEvidenceBundle(
            unit_id="u1",
            resolution=UnitResolution(status="not_found", reason_code="no_direct_evidence"),
        )],
    )
    monkeypatch.setattr(
        ask_executor,
        "memory_search",
        lambda *args, **kwargs: [{"id": "ep-1", "memory_type": "episodic", "content": "论文答辩已经完成", "sources": []}],
    )

    repaired = ask_executor.repair_missing_evidence("space", plan, result)

    assert repaired == ["u1"]
    assert result.bundles[0].resolution.status == "partial"
    assert result.bundles[0].evidence[0].evidence_role == "historical"


def test_evidence_resolver_selects_late_answer_bearing_span():
    from agent.ask_models import EvidenceItem
    from agent.evidence_resolver import resolve_bundle_spans

    unit = QueryUnit(**_unit(
        "u1", "慈善5K的个人最好成绩是多少？", "episodic_history", "个人最好成绩",
        memory_type="episodic", time_mode="history", evidence_mode="source_quote",
    ))
    filler = "这是与问题无关的训练日志。" * 180
    bundle = UnitEvidenceBundle(
        unit_id="u1",
        evidence=[EvidenceItem(
            evidence_id="note-5k", source_kind="note", note_id="note-5k",
            content="旧摘要", full_text=f"{filler} 最后确认：慈善5K个人最好成绩是25:50。",
        )],
        resolution=UnitResolution(status="resolved", reason_code="test", selected_evidence_ids=["note-5k"]),
    )

    resolve_bundle_spans([unit], [bundle])

    assert "25:50" in bundle.evidence[0].evidence_span
    assert "25:50" in bundle.evidence[0].fact_hints
    assert bundle.evidence[0].full_text is None


def test_hydration_expands_selected_note_itself_before_resolving(monkeypatch):
    from agent import ask_executor
    from agent.evidence_resolver import resolve_bundle_spans
    from agent.ask_models import EvidenceItem

    unit = QueryUnit(**_unit(
        "u1", "我拿到什么学位？", "semantic_current", "学位",
        memory_type="semantic", need_source_evidence=True,
    ))
    bundle = UnitEvidenceBundle(
        unit_id="u1",
        evidence=[EvidenceItem(
            evidence_id="note-degree", source_kind="note", note_id="note-degree", content="短摘要",
        )],
        resolution=UnitResolution(status="resolved", reason_code="test", selected_evidence_ids=["note-degree"]),
    )
    result = AskExecutionResult(bundles=[bundle], records_by_unit={"u1": []})
    monkeypatch.setattr(
        "agent.query_agent.get_note_for_evidence",
        lambda *_args, **_kwargs: {"id": "note-degree", "text": "I graduated with a Business Administration degree."},
    )

    hydrated = ask_executor.hydrate_selected_evidence("space", AskPlan(original_query=unit.question, units=[unit]), result)
    resolve_bundle_spans([unit], result.bundles)

    assert hydrated[0]["id"] == "note-degree"
    assert "Business Administration" in bundle.evidence[0].evidence_span


def test_answer_payload_contains_only_bounded_span_and_fact_hints():
    from agent import ask_workflow
    from agent.ask_models import EvidenceItem

    bundle = UnitEvidenceBundle(
        unit_id="u1",
        evidence=[EvidenceItem(
            evidence_id="note-1", source_kind="note", content="2023-05-27: 25:50",
            evidence_span="2023-05-27: 25:50", fact_hints=["2023-05-27", "25:50"],
        )],
        resolution=UnitResolution(status="resolved", reason_code="test", selected_evidence_ids=["note-1"]),
    )

    payload = ask_workflow._bundle_payload(bundle)

    assert payload["evidence"][0]["fact_hints"] == ["2023-05-27", "25:50"]
    assert "full_text" not in payload["evidence"][0]


def test_executor_isolates_one_domain_tool_failure(monkeypatch):
    from agent import ask_executor

    plan = AskPlan(
        original_query="任务和偏好",
        units=[
            QueryUnit(**_unit("u1", "任务状态", "task_state", "任务", memory_type="task")),
            QueryUnit(**_unit("u2", "喜欢什么", "preference_current", "喜欢", memory_type="preference")),
        ],
    )

    def _task_failure(*_args, **_kwargs):
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(ask_executor, "task_status_search", _task_failure)
    monkeypatch.setattr(
        ask_executor,
        "memory_search",
        lambda *_args, **_kwargs: [{"id": "pref-1", "memory_type": "preference", "content": "喜欢可乐", "sources": []}],
    )

    result = ask_executor.execute_ask_plan("space", plan)

    assert result.unit_errors["u1"].startswith("RuntimeError")
    assert next(bundle for bundle in result.bundles if bundle.unit_id == "u2").resolution.status == "resolved"


def test_repair_respects_single_round_budget(monkeypatch):
    from agent import ask_executor

    unit = QueryUnit(**_unit("u1", "论文答辩怎么样？", "task_state", "论文答辩", memory_type="task"))
    plan = AskPlan(original_query=unit.question, units=[unit])
    result = AskExecutionResult(bundles=[UnitEvidenceBundle(
        unit_id="u1", resolution=UnitResolution(status="not_found", reason_code="test"),
    )])
    monkeypatch.setattr(ask_executor.settings, "ASK_MAX_RETRIEVAL_ROUNDS", 1)

    repaired = ask_executor.repair_missing_evidence("space", plan, result)

    assert repaired == []


def test_fact_resolver_keeps_quote_grounded_atomic_facts(monkeypatch):
    from agent import fact_resolver
    from agent.ask_models import EvidenceItem

    unit = QueryUnit(**_unit(
        "u1", "我一共露营几天？", "episodic_history", "露营几天",
        memory_type="episodic", time_mode="all", evidence_mode="aggregate",
    ))
    bundle = UnitEvidenceBundle(
        unit_id="u1",
        evidence=[EvidenceItem(
            evidence_id="note-camping", source_kind="note",
            content="我在黄石露营了5天，并计划下月再去Big Sur。",
            evidence_span="我在黄石露营了5天，并计划下月再去Big Sur。",
        )],
        resolution=UnitResolution(status="partial", reason_code="test", selected_evidence_ids=["note-camping"]),
    )
    monkeypatch.setattr(fact_resolver, "complete_json", lambda **_kwargs: {"facts": [
        {
            "unit_id": "u1", "evidence_id": "note-camping", "quote": "我在黄石露营了5天",
            "claim": "黄石露营5天", "modality": "asserted", "item_key": "黄石露营", "quantity": 5,
        },
        {
            "unit_id": "u1", "evidence_id": "note-camping", "quote": "计划下月再去Big Sur",
            "claim": "下月计划去Big Sur", "modality": "planned",
        },
    ]})

    result = fact_resolver.resolve_evidence_facts(unit.question, AskPlan(original_query=unit.question, units=[unit]), [bundle])

    assert result.accepted == 2
    assert [fact.modality for fact in bundle.facts] == ["asserted", "planned"]
    assert "计划/愿望" in bundle.fact_summary


def test_fact_resolver_rejects_ungrounded_quote_and_quantity(monkeypatch):
    from agent import fact_resolver
    from agent.ask_models import EvidenceItem

    unit = QueryUnit(**_unit("u1", "有几件衣物？", "note_lookup", "几件", evidence_mode="aggregate"))
    bundle = UnitEvidenceBundle(
        unit_id="u1",
        evidence=[EvidenceItem(evidence_id="note-store", source_kind="note", content="干洗店有一件西装。")],
        resolution=UnitResolution(status="partial", reason_code="test", selected_evidence_ids=["note-store"]),
    )
    monkeypatch.setattr(fact_resolver, "complete_json", lambda **_kwargs: {"facts": [
        {
            "unit_id": "u1", "evidence_id": "note-store", "quote": "有三件衣物",
            "claim": "有三件衣物", "modality": "asserted", "quantity": 3,
        },
    ]})

    result = fact_resolver.resolve_evidence_facts(unit.question, AskPlan(original_query=unit.question, units=[unit]), [bundle])

    assert result.accepted == 0
    assert result.rejected_reasons["quote_not_grounded"] == 1
    assert bundle.facts == []


def test_validator_promotes_task_inventory_without_misrouting_preferences():
    from agent.ask_plan_validator import validate_ask_plan

    task_question = "列出我当前三个项目的状态。"
    task_plan = validate_ask_plan({
        "original_query": task_question,
        "units": [_unit("u1", task_question, "semantic_current", "列出我当前三个项目", facet="project")],
        "answer_mode": "list",
    }, question=task_question)
    assert task_plan.units[0].intent == "task_inventory"
    assert task_plan.units[0].memory_type == "task"

    preference_question = "列出我喜欢喝的饮料。"
    preference_plan = validate_ask_plan({
        "original_query": preference_question,
        "units": [_unit("u1", preference_question, "preference_current", "列出我喜欢喝的饮料")],
        "answer_mode": "list",
    }, question=preference_question)
    assert preference_plan.units[0].intent == "preference_current"


def test_validator_promotes_timeline_before_dispatch():
    from agent.ask_plan_validator import validate_ask_plan

    question = "论文答辩经历了哪些状态变化？"
    plan = validate_ask_plan({
        "original_query": question,
        "units": [_unit("u1", question, "task_state", "经历了哪些状态变化", memory_type="task", topic="论文答辩")],
        "answer_mode": "timeline",
    }, question=question)
    unit = plan.units[0]
    assert unit.intent == "memory_history"
    assert unit.topic == "论文答辩"
    assert unit.evidence_mode == "timeline"


def test_timeline_adapter_flattens_versions(monkeypatch):
    from agent import ask_executor
    import memory.repository

    monkeypatch.setattr(memory.repository, "get_memory_timeline", lambda *args, **kwargs: [{
        "id": "memory-1",
        "memory_id": "memory-1",
        "memory_type": "task",
        "content": "当前完成",
        "sources": [],
        "versions": [
            {"id": "version-1", "content": "待完成", "task_status": "todo", "valid_from": "2026-01-01", "source_note_id": "note-1"},
            {"id": "version-2", "content": "已完成", "task_status": "done", "valid_from": "2026-01-02", "source_note_id": "note-2"},
        ],
    }])
    unit = QueryUnit(**_unit("u1", "论文答辩经历如何？", "memory_history", "经历如何", topic="论文答辩", time_mode="history", evidence_mode="timeline"))
    tool, records = ask_executor._execute_domain_tool("space", unit, access_context=None)
    assert tool == "get_memory_timeline"
    assert [row["version_id"] for row in records] == ["version-1", "version-2"]
    bundle = ask_executor._resolve(unit, records, space_id="space", tool=tool)
    assert [item.source_kind for item in bundle.evidence] == ["memory_version", "memory_version"]
    assert [item.version_id for item in bundle.evidence] == ["version-1", "version-2"]


def test_task_inventory_is_bounded_and_acl_filtered(monkeypatch):
    from agent import ask_executor

    class Record:
        def __init__(self, data):
            self.data = data
        def to_dict(self):
            return self.data

    monkeypatch.setattr(ask_executor, "list_memories", lambda *args, **kwargs: [
        Record({"id": "visible", "memory_type": "task", "content": "公开任务"}),
        Record({"id": "hidden", "memory_type": "task", "content": "受限任务", "scope": {"visibility": "private"}}),
    ])
    monkeypatch.setattr("memory.access.memory_access_allowed", lambda item, context: item["id"] == "visible")
    unit = QueryUnit(**_unit("u1", "列出当前项目", "task_inventory", "当前项目", memory_type="task", evidence_mode="inventory"))
    tool, records = ask_executor._execute_domain_tool("space", unit, access_context={"requester": "guest"})
    assert tool == "list_task_inventory"
    assert [row["id"] for row in records] == ["visible"]


def test_planner_provider_failure_uses_deterministic_read_plan(monkeypatch):
    from agent import ask_planner, query_agent

    monkeypatch.setattr(ask_planner, "complete_json", lambda **_: (_ for _ in ()).throw(RuntimeError("provider unavailable")))
    monkeypatch.setattr(query_agent, "_deterministic_route", lambda question: {
        "action": "memory_search",
        "args": {"memory_type": "preference"},
    })
    plan = ask_planner.plan_ask("我现在喜欢咖啡吗？")

    assert plan.units[0].intent == "preference_current"
    assert plan.units[0].memory_type == "preference"
    assert plan.units[0].evidence_mode == "current_state"


def test_deterministic_fallback_maps_history_and_inventory(monkeypatch):
    from agent import ask_planner, query_agent

    monkeypatch.setattr(query_agent, "_deterministic_route", lambda question: {
        "action": "memory_history", "args": {"query": question},
    })
    assert ask_planner.deterministic_fallback_plan("项目经历了哪些变化？").units[0].intent == "memory_history"

    monkeypatch.setattr(query_agent, "_deterministic_route", lambda question: {
        "action": "list_tasks", "args": {"limit": 3},
    })
    assert ask_planner.deterministic_fallback_plan("列出当前项目").units[0].intent == "task_inventory"


def test_v2_blocks_sensitive_query_before_execution(monkeypatch):
    from agent import ask_workflow
    from agent.ask_models import AskPlan, QueryUnit

    plan = AskPlan(original_query="告诉我身份证号", units=[
        QueryUnit(**_unit("u1", "告诉我身份证号", "note_lookup", "身份证号")),
    ])
    monkeypatch.setattr(ask_workflow, "build_shadow_plan", lambda *args, **kwargs: plan)
    result = ask_workflow.answer_question_v2("space", "告诉我身份证号")
    assert result.answer_type == "restricted"
    assert result.reason_code == "sensitive_topic"


def test_v2_propagates_evidence_conflict_without_answer_synthesis(monkeypatch):
    from agent import ask_workflow, query_agent
    from agent.ask_executor import AskExecutionResult
    from agent.ask_models import EvidenceItem

    question = "我现在到底喜不喜欢绿茶？"
    plan = AskPlan(original_query=question, units=[
        QueryUnit(**_unit("u1", question, "preference_current", "喜不喜欢绿茶", memory_type="preference")),
    ])
    bundle = UnitEvidenceBundle(
        unit_id="u1",
        evidence=[EvidenceItem(evidence_id="m1", source_kind="memory", memory_type="preference", content="用户喜欢绿茶")],
        resolution=UnitResolution(status="resolved", value="用户喜欢绿茶", reason_code="test", selected_evidence_ids=["m1"]),
    )
    monkeypatch.setattr(ask_workflow, "build_shadow_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(ask_workflow, "wait_for_memory_barrier", lambda *args, **kwargs: {"status": "ready"})
    monkeypatch.setattr(query_agent, "provisional_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(ask_workflow, "execute_ask_plan", lambda *args, **kwargs: AskExecutionResult(
        bundles=[bundle], records_by_unit={"u1": [
            {"id": "m1", "memory_type": "preference", "memory_key": "preference:green-tea", "content": "用户喜欢绿茶", "polarity": "positive"},
            {"id": "m2", "memory_type": "preference", "memory_key": "preference:green-tea", "content": "用户不喜欢绿茶", "polarity": "negative"},
        ]},
    ))
    monkeypatch.setattr(ask_workflow, "hydrate_selected_evidence", lambda *args, **kwargs: [])
    result = ask_workflow.answer_question_v2("space", question)
    assert result.answer_type == "conflict"
    assert "冲突" in result.answer


def test_executor_relevance_gate_rejects_unrelated_preference_candidate():
    from agent.ask_executor import _record_relevant_to_unit

    unit = QueryUnit(**_unit("u1", "我最喜欢的电影是什么？", "preference_current", "最喜欢的电影", memory_type="preference"))
    assert not _record_relevant_to_unit(unit, {"content": "用户喜欢咖啡", "memory_type": "preference"})


def test_executor_relevance_gate_allows_general_preference_question():
    from agent.ask_executor import _record_relevant_to_unit

    unit = QueryUnit(**_unit("u1", "我喜欢喝什么？", "preference_current", "喜欢喝什么", memory_type="preference"))
    assert _record_relevant_to_unit(unit, {"content": "用户喜欢可乐", "memory_type": "preference"})


def test_validator_routes_ambiguous_task_status_to_inventory():
    plan = validate_ask_plan(
        {"original_query": "那个评测现在怎么样了？", "units": [
            _unit("u1", "那个评测现在怎么样了？", "note_lookup", "那个评测现在怎么样了？", evidence_mode="source_quote"),
        ]},
        question="那个评测现在怎么样了？",
    )
    assert plan.units[0].intent == "task_inventory"


def test_pending_review_is_a_conflict_even_without_conflict_word():
    from agent.query_agent import decide_answer

    decision = decide_answer("我喜欢绿茶吗？", None, None, current_evidence=[
        {"id": "m1", "memory_type": "preference", "memory_key": "preference:green-tea", "status": "active", "polarity": "positive"},
        {"id": "m2", "memory_type": "preference", "memory_key": "preference:green-tea", "status": "pending_review", "polarity": "negative"},
    ])
    assert decision.answer_type == "conflict"


def test_planner_uses_its_bounded_timeout(monkeypatch):
    from agent import ask_planner

    captured = {}
    question = "我现在住哪？"
    monkeypatch.setattr(ask_planner.settings, "ASK_PLANNER_TIMEOUT_SECONDS", 7)
    monkeypatch.setattr(ask_planner, "complete_json", lambda **kwargs: captured.update(kwargs) or {
        "original_query": question,
        "units": [_unit("u1", question, "semantic_current", "我现在住哪", memory_type="semantic", facet="location")],
    })

    ask_planner.plan_ask(question)

    assert captured["timeout_seconds"] == 7


def test_answer_synthesis_uses_its_bounded_timeout(monkeypatch):
    from agent import ask_workflow
    from agent.ask_models import EvidenceItem

    captured = {}
    unit = QueryUnit(**_unit("u1", "我喜欢喝什么？", "preference_current", "喜欢喝什么", memory_type="preference"))
    plan = AskPlan(original_query=unit.question, units=[unit])
    bundle = UnitEvidenceBundle(
        unit_id="u1",
        evidence=[EvidenceItem(evidence_id="m1", source_kind="memory", memory_type="preference", content="用户喜欢可乐")],
        resolution=UnitResolution(status="resolved", value="用户喜欢可乐", reason_code="test", selected_evidence_ids=["m1"]),
    )
    monkeypatch.setattr(ask_workflow.settings, "ASK_ANSWER_TIMEOUT_SECONDS", 9)
    monkeypatch.setattr(ask_workflow, "complete_json", lambda **kwargs: captured.update(kwargs) or {
        "unit_answers": [{"unit_id": "u1", "answer": "你喜欢可乐", "evidence_ids": ["m1"]}],
        "final_answer": "你喜欢可乐",
    })

    assert ask_workflow._answer_from_bundles(unit.question, plan, [bundle]) == "你喜欢可乐"
    assert captured["timeout_seconds"] == 9


def test_executor_returns_partial_for_over_budget_unit(monkeypatch):
    import time
    from agent import ask_executor

    unit = QueryUnit(**_unit("u1", "简历做完了吗？", "task_state", "简历做完", memory_type="task"))
    monkeypatch.setattr(ask_executor.settings, "ASK_EXECUTOR_TIMEOUT_SECONDS", 0.1)

    def slow_tool(*_args, **_kwargs):
        time.sleep(0.35)
        return "search_task_state", []

    monkeypatch.setattr(ask_executor, "_execute_domain_tool", slow_tool)
    started = time.monotonic()
    result = ask_executor.execute_ask_plan("space", AskPlan(original_query=unit.question, units=[unit]))

    assert time.monotonic() - started < 0.25
    assert result.timed_out_units == ["u1"]
    assert result.unit_errors["u1"] == "executor_timeout"
    assert result.bundles[0].resolution.reason_code == "executor_timeout"


def test_answer_contract_ignores_free_final_and_exports_only_rendered_claims(monkeypatch):
    from agent import ask_workflow
    from agent.ask_models import EvidenceItem

    plan = AskPlan(
        original_query="where do I live",
        units=[QueryUnit(**_unit("u1", "where do I live", "semantic_current", "where", memory_type="semantic", facet="location"))],
    )
    bundles = [
        UnitEvidenceBundle(
            unit_id="u1",
            evidence=[
                EvidenceItem(evidence_id="mem-city", source_kind="memory", memory_type="semantic", memory_id="m1", content="user lives in Shanghai", source_note_ids=["s1"]),
                EvidenceItem(evidence_id="note-city", source_kind="note", memory_type="semantic", note_id="n1", content="user lives in Shanghai", source_note_ids=["s1"]),
            ],
            resolution=UnitResolution(status="resolved", value="user lives in Shanghai", reason_code="test", selected_evidence_ids=["mem-city"]),
        )
    ]
    monkeypatch.setattr(ask_workflow, "complete_json", lambda **_: {
        "unit_answers": [{
            "unit_id": "u1",
            "answer": "You live in Shanghai.",
            "evidence_ids": ["mem-city", "note-city"],
            "claims": [
                {"text": "You live in Shanghai.", "evidence_ids": ["mem-city"]},
                {"text": "You live in Shanghai.", "evidence_ids": ["note-city"]},
            ],
        }],
        "final_answer": "You live in Shanghai and should move soon.",
    })

    answer, claims = ask_workflow._answer_with_claims_from_bundles("where do I live", plan, bundles)

    assert answer == "You live in Shanghai."
    assert claims == [{
        "text": "You live in Shanghai.",
        "memory_ids": ["m1"],
        "version_ids": [],
        "source_ids": ["s1"],
        "support_role": "current",
    }]


def test_v2_non_factual_decision_exports_no_candidate_claims(monkeypatch):
    from types import SimpleNamespace
    from agent import ask_workflow, query_agent
    from agent.ask_models import EvidenceItem

    plan = AskPlan(
        original_query="do I like tea",
        units=[QueryUnit(**_unit("u1", "do I like tea", "preference_current", "like tea", memory_type="preference"))],
    )
    bundle = UnitEvidenceBundle(
        unit_id="u1",
        evidence=[
            EvidenceItem(evidence_id="like", source_kind="memory", memory_type="preference", memory_id="m1", content="user likes tea"),
            EvidenceItem(evidence_id="dislike", source_kind="memory", memory_type="preference", memory_id="m2", content="user dislikes tea"),
        ],
        resolution=UnitResolution(status="conflict", reason_code="test", conflicting_evidence_ids=["like", "dislike"]),
    )
    monkeypatch.setattr(ask_workflow, "build_shadow_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(ask_workflow, "wait_for_memory_barrier", lambda *args, **kwargs: {"status": "ready", "waited_ms": 0})
    monkeypatch.setattr(query_agent, "provisional_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(ask_workflow, "execute_ask_plan", lambda *args, **kwargs: AskExecutionResult(
        bundles=[bundle],
        records_by_unit={"u1": [{"id": "m1", "memory_type": "preference", "content": "user likes tea"}]},
    ))
    monkeypatch.setattr(ask_workflow, "hydrate_selected_evidence", lambda *args, **kwargs: [])
    monkeypatch.setattr(ask_workflow, "resolve_evidence_facts", lambda *args, **kwargs: SimpleNamespace(accepted=0, rejected=0, rejected_reasons={}))
    monkeypatch.setattr(query_agent, "decide_answer", lambda *args, **kwargs: SimpleNamespace(answer_type="conflict", reason_code="conflict"))

    result = ask_workflow.answer_question_v2("space", "do I like tea")

    assert result.answer_type == "conflict"
    assert result.claims == []


def test_v2_not_found_raw_candidate_is_not_promoted_to_answer_or_claim(monkeypatch):
    """A broad retrieval candidate is not evidence when resolution is not_found."""
    from agent import ask_workflow, query_agent

    question = "我最喜欢的电影是什么？"
    plan = AskPlan(original_query=question, units=[QueryUnit(**_unit(
        "u1", question, "preference_current", question,
        memory_type="preference", topic=question,
    ))])
    bundle = UnitEvidenceBundle(
        unit_id="u1",
        resolution=UnitResolution(status="not_found", reason_code="no_direct_evidence"),
    )
    monkeypatch.setattr(ask_workflow, "build_shadow_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr(ask_workflow, "wait_for_memory_barrier", lambda *args, **kwargs: {"status": "ready", "waited_ms": 0})
    monkeypatch.setattr(query_agent, "provisional_search", lambda *args, **kwargs: [])
    monkeypatch.setattr(ask_workflow, "execute_ask_plan", lambda *args, **kwargs: AskExecutionResult(
        bundles=[bundle],
        records_by_unit={"u1": [{"id": "coffee", "memory_type": "preference", "content": "用户喜欢咖啡", "sources": []}]},
    ))
    monkeypatch.setattr(ask_workflow, "hydrate_selected_evidence", lambda *args, **kwargs: [])
    monkeypatch.setattr(ask_workflow, "complete_json", lambda **_: (_ for _ in ()).throw(AssertionError("no-answer must not call answer LLM")))

    result = ask_workflow.answer_question_v2("space", question)

    assert result.answer_type == "no_answer"
    assert result.claims == []
    assert "没有找到相关记录" in result.answer


def test_preference_topic_does_not_degrade_to_global_inventory():
    from agent.ask_executor import _record_relevant_to_unit

    unit = QueryUnit(**_unit(
        "u1", "我最喜欢的电影是什么？", "preference_current", "我最喜欢的电影是什么？",
        memory_type="preference", topic="电影",
    ))
    assert not _record_relevant_to_unit(unit, {"content": "用户喜欢咖啡", "memory_type": "preference"})


def test_answer_contract_retries_once_and_never_falls_back_to_raw_evidence(monkeypatch):
    from agent import ask_workflow
    from agent.ask_models import EvidenceItem

    unit = QueryUnit(**_unit("u1", "Where do I live?", "note_lookup", "Where do I live?"))
    plan = AskPlan(original_query=unit.question, units=[unit])
    bundles = [UnitEvidenceBundle(
        unit_id="u1",
        evidence=[EvidenceItem(evidence_id="note-city", source_kind="note", content="User lives in Shanghai.")],
        resolution=UnitResolution(status="resolved", value="User lives in Shanghai.", reason_code="test", selected_evidence_ids=["note-city"]),
    )]
    calls = {"count": 0}
    prompts: list[str] = []

    def fake_complete_json(**_kwargs):
        calls["count"] += 1
        prompts.append(_kwargs["system_prompt"])
        if calls["count"] == 1:
            return {"unit_answers": [{"unit_id": "u1", "answer": "You live in Shanghai.", "evidence_ids": ["wrong-id"]}]}
        return {"unit_answers": [{"unit_id": "u1", "answer": "You live in Shanghai.", "evidence_ids": ["note-city"]}]}

    monkeypatch.setattr(ask_workflow, "complete_json", fake_complete_json)
    answer, claims, diagnostic = ask_workflow._answer_with_claims_from_bundles_detailed(unit.question, plan, bundles)

    assert answer == "You live in Shanghai."
    assert claims
    assert calls["count"] == 2
    assert prompts[1] == ask_workflow.ASK_ANSWER_CONTRACT_REPAIR_PROMPT
    assert diagnostic["status"] == "repaired"


def test_answer_contract_failure_returns_controlled_message_not_raw_evidence(monkeypatch):
    from agent import ask_workflow
    from agent.ask_models import EvidenceItem

    unit = QueryUnit(**_unit("u1", "Where do I live?", "note_lookup", "Where do I live?"))
    plan = AskPlan(original_query=unit.question, units=[unit])
    bundles = [UnitEvidenceBundle(
        unit_id="u1",
        evidence=[EvidenceItem(evidence_id="note-city", source_kind="note", content="User lives in Shanghai. Secret unrelated raw context.")],
        resolution=UnitResolution(status="resolved", value="User lives in Shanghai. Secret unrelated raw context.", reason_code="test", selected_evidence_ids=["note-city"]),
    )]
    monkeypatch.setattr(ask_workflow, "complete_json", lambda **_kwargs: {"unit_answers": []})

    answer, claims, diagnostic = ask_workflow._answer_with_claims_from_bundles_detailed(unit.question, plan, bundles)

    assert "可验证回答" in answer
    assert "Secret unrelated" not in answer
    assert claims == []
    assert diagnostic["status"] == "failed"
    assert diagnostic["attempts"] == 2
