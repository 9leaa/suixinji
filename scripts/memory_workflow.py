"""Preference maintenance and quality-case review. Run with python -m scripts.memory_workflow."""
import argparse
import json
from pathlib import Path

from memory import workflow_state as state
from memory.preference_cards import run_consolidation
from memory.workflow import export_cases, review_issue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--space', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('pending')
    commands.add_parser('issues')
    commands.add_parser('consolidate')
    migrate = commands.add_parser('migrate')
    migrate.add_argument('--apply', action='store_true', help='默认只列出需要迁移的旧偏好')
    review = commands.add_parser('review')
    review.add_argument('id')
    review.add_argument('--status', required=True, choices=['confirmed', 'normal', 'insufficient'])
    review.add_argument('--expected', help='人工确认的期望结果 JSON 文件')
    review.add_argument('--reviewer', required=True)
    export = commands.add_parser('export')
    export.add_argument('path')
    export.add_argument('--replacements', help='可选脱敏替换映射 JSON 文件；导出默认仅供自己复核')
    args = parser.parse_args()
    if args.command in {'pending', 'issues'}:
        result = state.list_items(args.space, 'pending' if args.command == 'pending' else 'issue')
    elif args.command == 'consolidate':
        result = run_consolidation(args.space, force=True)
    elif args.command == 'migrate':
        from memory.preference_migration import migrate_legacy
        result = migrate_legacy(args.space, apply=args.apply)
    elif args.command == 'review':
        expected = json.loads(Path(args.expected).read_text()) if args.expected else None
        result = review_issue(args.space, args.id, status=args.status, expected=expected, reviewer=args.reviewer)
    else:
        replacements = json.loads(Path(args.replacements).read_text()) if args.replacements else None
        result = {'exported': export_cases(args.space, args.path, replacements=replacements)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
