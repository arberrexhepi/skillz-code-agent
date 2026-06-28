from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from context_tree import ContextTree
from issue_facts import IssueFactLedger
from tree_commands import CommandResult, StrategyStep, TreeCommandParser, _evaluate_placeholder_expression, execute_multi
from tree_loop import extract_commands


class PlaceholderTransformTests(unittest.TestCase):
    def test_match_transform_allows_parentheses_inside_regex(self):
        steps = {
            "s1": StrategyStep(
                label="s1",
                commands=["fact demo"],
                results=[CommandResult(ok=True, output="route check failed: 503 service unavailable", command_type="read")],
            )
        }

        value = _evaluate_placeholder_expression(
            "s1.match(/failed: (\\d+) service unavailable/)[1]",
            steps,
        )

        self.assertEqual(value, "503")

    def test_match_transform_allows_escaped_closing_paren_inside_regex(self):
        steps = {
            "s1": StrategyStep(
                label="s1",
                commands=["fact demo"],
                results=[CommandResult(ok=True, output="error: expected call foo)", command_type="read")],
            )
        }

        value = _evaluate_placeholder_expression(
            r"s1.match(/call foo\)/)[0]",
            steps,
        )

        self.assertEqual(value, "call foo)")


class IssueListCommandTests(unittest.TestCase):
    def test_extract_commands_ignores_bare_heredoc_terminator(self):
        self.assertEqual(
            extract_commands(">>th: done\n>>pl: finish\n>>>"),
            ">>th: done\n>>pl: finish",
        )

    def test_parse_multi_command_ignores_stray_heredoc_terminator(self):
        tree = ContextTree(Path.cwd())
        parser = TreeCommandParser(tree)

        results, annotations = execute_multi(parser, ">>>\ncat /facts/missing")
        self.assertEqual(len(results), 1)
        self.assertEqual(annotations, [])

    def test_list_issues_includes_durable_repo_facts_issue_state(self):
        ledger = IssueFactLedger.empty()
        issue = ledger.create_issue(
            request_summary="Fix upload cleanup",
            plan_summary="Fix upload cleanup",
            activate=True,
        )
        payload = ledger.planner_payload(path="/repo/repo_facts.md")
        tree = ContextTree(Path.cwd())
        parser = TreeCommandParser(tree, get_issue_state=lambda: payload)

        result = parser.parse_and_execute("list-issues")

        self.assertTrue(result.ok)
        self.assertIn("Durable issues from repo_facts", result.output)
        self.assertIn(issue.issue_id, result.output)

    def test_show_issue_prefers_durable_issue_over_run_issue_id_collision(self):
        ledger = IssueFactLedger.empty()
        durable = ledger.create_issue(
            request_summary="Closed durable issue",
            plan_summary="Closed durable issue",
            activate=True,
        )
        ledger.close_issue(durable.issue_id, note="done")
        payload = ledger.planner_payload(path="/repo/repo_facts.md")
        tree = ContextTree(Path.cwd())
        tree.set_fact(tree.LOG_ISSUES_ROOT, durable.issue_id, "status", "open")
        tree.set_fact(tree.LOG_ISSUES_ROOT, durable.issue_id, "summary", "Open run issue")
        parser = TreeCommandParser(tree, get_issue_state=lambda: payload)

        durable_result = parser.parse_and_execute(f"show-issue {durable.issue_id}")
        run_result = parser.parse_and_execute(f"show-run-issue {durable.issue_id}")

        self.assertIn(f"Issue {durable.issue_id} [closed]", durable_result.output)
        self.assertIn(f"Run Issue {durable.issue_id} [open]", run_result.output)

    def test_sync_facts_preserves_current_run_log_issues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tree = ContextTree(Path(tmpdir))
            tree.set_fact(
                tree.LOG_ISSUES_ROOT,
                "run-001",
                "summary",
                "Typecheck failed",
            )

            tree.sync_facts([
                SimpleNamespace(
                    issue_id="issue-001",
                    fact_type="goal",
                    key="target_file",
                    value="src/app.py",
                    updated_step=1,
                    updated_run_id=1,
                )
            ])

        issues = tree.list_log_issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["id"], "run-001")
        self.assertEqual(issues[0]["summary"], "Typecheck failed")

    def test_empty_sync_facts_clears_stale_durable_facts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tree = ContextTree(Path(tmpdir))
            tree.sync_facts([
                SimpleNamespace(
                    issue_id="issue-001",
                    fact_type="goal",
                    key="target_file",
                    value="src/app.py",
                    updated_step=1,
                    updated_run_id=1,
                )
            ])

            tree.sync_facts([])

        self.assertEqual(tree.cat("/facts/issue-001/goal/target_file"), "[not found: /facts/issue-001/goal/target_file]")


if __name__ == "__main__":
    unittest.main()
