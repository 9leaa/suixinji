"""Build the deterministic 360-case Memory V2 quality set used by baselines."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


OUTPUT = Path(__file__).resolve().parent / "memory" / "quality_cases.jsonl"


def _extraction_cases() -> list[dict[str, object]]:
    positives = [
        ("preference", "我喜欢喝{item}。", ["{item}"], "explicit_preference"),
        ("preference", "我不喜欢吃{item}，以后尽量避开。", ["{item}"], "negative_preference"),
        ("preference", "工作日我习惯在{item}后散步。", ["{item}"], "habit"),
        ("task", "记得完成{item}。", ["{item}"], "task_marker"),
        ("task", "请把{item}列为待办。", ["{item}"], "todo_marker"),
        ("semantic", "我正在学习{item}。", ["{item}"], "learning_fact"),
        ("semantic", "我目前负责{item}。", ["{item}"], "responsibility"),
        ("semantic", "我住在{item}。", ["{item}"], "location"),
        ("episodic", "昨天我去了{item}。", ["{item}"], "recent_event"),
        ("episodic", "今天我完成了{item}。", ["{item}"], "recent_event"),
    ]
    items = [
        "牛奶", "绿茶", "苹果", "跑步", "项目周报", "Python", "Redis", "上海", "图书馆", "实验记录",
    ]
    negatives = [
        ("闲聊问候", "你好，今天过得怎么样？"),
        ("纯问题", "你能解释一下什么是数据库索引吗？"),
        ("临时天气", "今天外面好像有点冷。"),
        ("短确认", "好的，收到。"),
        ("泛泛计划", "以后有时间再看看吧。"),
        ("引用内容", "朋友说他喜欢喝咖啡，但那不是我的信息。"),
        ("系统操作", "/help"),
        ("无关感叹", "这个接口终于响应了！"),
        ("不确定猜测", "我可能明天会去公园，也不一定。"),
        ("泛化观点", "很多人都喜欢旅行。"),
    ]
    cases: list[dict[str, object]] = []
    index = 0
    for memory_type, template, include, category in positives:
        for item in items[:6]:
            text = template.format(item=item)
            cases.append(
                {
                    "case_id": f"extract-{index:03d}",
                    "kind": "extraction",
                    "category": category,
                    "text": text,
                    "expected_types": [memory_type],
                    "should_store": True,
                    "must_include_any": [term.format(item=item) for term in include],
                }
            )
            index += 1
    for category, text in negatives:
        for variant in range(6):
            cases.append(
                {
                    "case_id": f"extract-{index:03d}",
                    "kind": "extraction",
                    "category": category,
                    "text": text if variant == 0 else f"{text}（第{variant + 1}次）",
                    "expected_types": [],
                    "should_store": False,
                }
            )
            index += 1
    return cases


def _relation_cases() -> list[dict[str, object]]:
    templates = {
        "same": [
            ("我喜欢喝{item}。", "我喜欢喝{item}。", "preference", "preference"),
            ("我正在学习{item}。", "最近我主要学习{item}。", "semantic", "semantic"),
            ("记得完成{item}。", "待办是完成{item}。", "task", "task"),
        ],
        "merge": [
            ("我喜欢喝{item}。", "我喜欢喝{item}，通常不加糖。", "preference", "preference"),
            ("我正在学习{item}。", "我最近还在研究{item}的实践。", "semantic", "semantic"),
            ("记得完成{item}。", "还要给{item}补充测试。", "task", "task"),
        ],
        "update_task": [
            ("记得完成{item}。", "{item}已经完成了。", "task", "task"),
            ("记得处理{item}。", "{item}正在进行中。", "task", "task"),
            ("处理{item}遇到阻塞。", "{item}现在可以继续了。", "task", "task"),
        ],
        "supersede": [
            ("我住在北京。", "我已经搬到上海了。", "semantic", "semantic"),
            ("我正在学习Java。", "我现在改学Python了。", "semantic", "semantic"),
            ("我喜欢喝牛奶。", "我现在讨厌喝牛奶了。", "preference", "preference"),
        ],
        "conflict": [
            ("我喜欢远程工作。", "我更喜欢去办公室工作。", "preference", "preference"),
            ("我住在北京。", "我住在上海。", "semantic", "semantic"),
            ("{item}没有问题。", "{item}仍然有问题。", "semantic", "semantic"),
        ],
        "new": [
            ("我喜欢喝牛奶。", "我正在学习Redis。", "preference", "semantic"),
            ("记得完成项目周报。", "我昨天去了图书馆。", "task", "episodic"),
            ("我住在北京。", "我喜欢吃苹果。", "semantic", "preference"),
        ],
    }
    items = ["牛奶", "绿茶", "项目周报", "Redis", "实验记录", "报告", "数据库", "日语", "跑步", "读书"]
    cases: list[dict[str, object]] = []
    index = 0
    for expected_relation, variants in templates.items():
        for variant in range(20):
            old_template, new_template, old_type, new_type = variants[variant % len(variants)]
            item = items[variant % len(items)]
            old = old_template.format(item=item)
            new = new_template.format(item=item)
            cases.append(
                {
                    "case_id": f"relation-{index:03d}",
                    "kind": "relation",
                    "category": expected_relation,
                    "old": old,
                    "new": new,
                    "old_type": old_type,
                    "new_type": new_type,
                    "expected_relation": expected_relation,
                    "destructive": expected_relation in {"merge", "update_task", "supersede", "conflict"},
                }
            )
            index += 1
    return cases


def _retrieval_cases() -> list[dict[str, object]]:
    topics = [
        ("preference", "喜欢喝{item}", "我想知道饮品偏好", "{item}"),
        ("preference", "不喜欢吃{item}", "哪些食物需要避开", "{item}"),
        ("semantic", "正在学习{item}", "最近学习的技术是什么", "{item}"),
        ("semantic", "住在{item}", "当前居住地", "{item}"),
        ("task", "记得完成{item}", "当前待办", "{item}"),
        ("episodic", "昨天去了{item}", "最近去过哪里", "{item}"),
    ]
    values = ["牛奶", "苹果", "Python", "上海", "项目周报", "图书馆", "绿茶", "Redis", "北京", "实验记录"]
    cases: list[dict[str, object]] = []
    index = 0
    for memory_type, target_template, query, marker in topics:
        for variant in range(10):
            value = values[variant]
            target_id = f"target-{index:03d}"
            distractors = [
                {"id": f"distractor-{index}-{offset}", "content": content, "type": distractor_type}
                for offset, (distractor_type, content) in enumerate(
                    [
                        ("semantic", "我在整理项目资料"),
                        ("preference", "我喜欢周末散步"),
                        ("task", "记得备份数据"),
                        ("episodic", "上周参加了会议"),
                    ]
                )
            ]
            cases.append(
                {
                    "case_id": f"retrieval-{index:03d}",
                    "kind": "retrieval",
                    "category": memory_type,
                    "memory_type": memory_type,
                    "query": query,
                    "marker": marker.format(item=value),
                    "target": {"id": target_id, "content": target_template.format(item=value), "type": memory_type},
                    "distractors": distractors,
                }
            )
            index += 1
    return cases


def _e2e_cases() -> list[dict[str, object]]:
    templates = [
        ("preference", ["我喜欢喝牛奶。"], "我喜欢喝什么", "牛奶"),
        ("preference", ["我喜欢喝牛奶。", "我讨厌喝牛奶。"], "我喜欢喝什么", None),
        ("semantic", ["我住在北京。", "我已经搬到上海了。"], "我现在住在哪里", "上海"),
        ("task", ["记得完成项目周报。", "项目周报已经完成了。"], "当前待办是什么", "项目周报"),
        ("semantic", ["我正在学习Java。", "我现在改学Python了。"], "我现在学习什么", "Python"),
        ("episodic", ["昨天我去了图书馆。"], "我最近去了哪里", "图书馆"),
    ]
    cases: list[dict[str, object]] = []
    for index in range(60):
        memory_type, messages, query, marker = templates[index % len(templates)]
        expected_no_result = marker is None
        cases.append(
            {
                "case_id": f"e2e-{index:03d}",
                "kind": "e2e",
                "category": memory_type,
                "messages": messages,
                "query": query,
                "expected_no_result": expected_no_result,
                "must_include": marker or "",
            }
        )
    return cases


def build_cases() -> list[dict[str, object]]:
    cases = _extraction_cases() + _relation_cases() + _retrieval_cases() + _e2e_cases()
    assert len(cases) == 360, len(cases)
    assert len({str(case["case_id"]) for case in cases}) == len(cases)
    return cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    cases = build_cases()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "cases": len(cases)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
