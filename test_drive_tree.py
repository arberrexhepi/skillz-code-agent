#!/usr/bin/env python3
"""
Interactive test drive for the ContextTree system.

Run:  python3 test_drive_tree.py

This indexes your actual workspace and drops you into a REPL
where you can type tree commands and see results — exactly
what the agent would see, but you're the agent.
"""

import sys
from pathlib import Path

from context_tree_bridge import ContextTreeBridge
from tree_commands import is_strategy, format_strategy_results


def main():
    root = Path(__file__).parent.resolve()

    # Fake callbacks — in real integration these come from WorkingFolderAgent
    bridge = ContextTreeBridge(
        workspace_root=root,
        get_fact_records=lambda: [],
        get_memory_items=lambda: [],
        get_status=lambda: {
            "task_satisfied": False,
            "edit_batch_mode": False,
            "completion_check_pending": False,
            "step": 0,
        },
    )

    # Register a sample skill
    bridge.register_skill(
        "project_summary",
        "Get a quick summary of this project",
        cache="Python agent with ContextTree-based in-context OS.\nMounts: /repo, /facts, /memory, /status, /skills",
    )
    bridge.register_skill(
        "count_py",
        "Count Python files in the repo",
        handler=lambda: str(len(bridge.tree.find("/repo", "*.py"))) + " Python files found",
    )

    # Seed some facts so /facts isn't empty
    bridge.tree.set_fact("demo", "architecture", "entrypoint", "main.py is the worker agent entry point")
    bridge.tree.set_fact("demo", "architecture", "planner", "planner.py orchestrates discovery → plan → goals")
    bridge.tree.set_fact("demo", "goal", "current", "Test drive the ContextTree system")

    print("=" * 60)
    setup = bridge.setup()
    print(f"Indexed {setup['files_indexed']} files, {setup['fact_count']} facts, {setup['skills']} skills")
    print("=" * 60)

    # Show what the prompt block looks like
    print("\n--- PROMPT BLOCK (what replaces 15 sections) ---\n")
    print(bridge.render_for_prompt(repo_depth=2))

    print("\n--- COMMAND GRAMMAR (what replaces JSON schema) ---\n")
    print(bridge.render_command_grammar())

    print("\n" + "=" * 60)
    print("REPL ready. Type tree commands. Multi-line: separate with newlines.")
    print("Type 'prompt' to see the full prompt block.")
    print("Type 'quit' to exit.")
    print("=" * 60 + "\n")

    # Example commands to try
    examples = [
        "ls /repo",
        "ls /repo depth=2",
        "cat /repo/context_tree.py:1-20",
        "find /repo *.py",
        "grep /facts \"planner\"",
        "stat /repo/main.py",
        "cat /facts/demo/architecture/entrypoint",
        "cat /status/step",
        "ls /skills",
        "skill project_summary",
        "skill count_py",
        "fact demo/goal/next Write integration tests",
        "# multi-command: reads are free",
        "ls /repo\ncat /repo/README.md\nfind /repo *.ts",
        "# strategy DAG pipeline:",
        "s1: cat /repo/README.md, ls /repo\\ns1 -> s2: stat /repo/main.py",
    ]
    print("Try these:")
    for ex in examples:
        if ex.startswith("#"):
            print(f"  {ex}")
        else:
            print(f"  {ex}")
    print()

    while True:
        try:
            raw = input("tree> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not raw:
            continue
        if raw == "quit":
            break
        if raw == "prompt":
            print(bridge.render_for_prompt(repo_depth=2))
            continue

        # Strategy DAG — show per-label results
        if is_strategy(raw):
            by_label = bridge.execute_strategy_full(raw)
            print(format_strategy_results(by_label))
            continue

        results = bridge.execute(raw)
        for r in results:
            tag = "READ" if r.command_type == "read" else "TOOL" if r.needs_tool else r.command_type.upper()
            ok_mark = "✓" if r.ok else "✗"
            print(f"\n[{ok_mark} {tag}] ", end="")
            if r.needs_tool:
                print(f"→ would dispatch: {r.tool_action}")
            print(r.output)


if __name__ == "__main__":
    main()
