from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from context_tree import ContextTree
from issue_facts import IssueFactLedger
from tree_commands import CommandResult, StrategyStep, TreeCommandParser, _evaluate_placeholder_expression, execute_multi, parse_strategy
from tree_loop import extract_commands


class PlaceholderTransformTests(unittest.TestCase):
    def test_dotted_strategy_label_placeholder_resolves_longest_step_label(self):
        steps = {
            "s1.a": StrategyStep(
                label="s1.a",
                commands=["fact demo"],
                results=[CommandResult(ok=True, output="alpha output", command_type="read")],
            ),
            "s1": StrategyStep(
                label="s1",
                commands=["fact demo"],
                results=[CommandResult(ok=True, output="base output", command_type="read")],
            ),
        }

        value = _evaluate_placeholder_expression("s1.a.stdout", steps)

        self.assertEqual(value, "alpha output")

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


class ContextTreeHydrationTests(unittest.TestCase):
    def test_beta_reads_hydrate_workspace_paths_omitted_by_index_cap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "README.md").write_text("# Workspace\n", encoding="utf-8")
            target = root / "packages" / "chat" / "src" / "index.ts"
            target.parent.mkdir(parents=True)
            target.write_text("export const chat = true;\n", encoding="utf-8")

            tree = ContextTree(root)
            tree.index_repo(max_files=1)
            parser = TreeCommandParser(tree)

            listing = parser.parse_and_execute("ls /repo/packages depth=1")
            symbols = parser.parse_and_execute("symbols /repo/packages/chat/src/index.ts")
            content = parser.parse_and_execute("cat /repo/packages/chat/src/index.ts:1-50")
            metadata = parser.parse_and_execute("stat /repo/packages/chat/src/index.ts")

            self.assertIn('"name": "chat/"', listing.output)
            self.assertIn('"name": "chat"', symbols.output)
            self.assertIn("export const chat = true", content.output)
            self.assertIn('"type": "file"', metadata.output)

    def test_repo_hydration_rejects_paths_outside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(tmpdir)
            (root / "README.md").write_text("# Workspace\n", encoding="utf-8")
            outside = Path(outside_dir) / "secret.txt"
            outside.write_text("system secret\n", encoding="utf-8")
            (root / "external.txt").symlink_to(outside)

            tree = ContextTree(root)
            tree.index_repo(max_files=1)

            self.assertEqual(tree.cat("/repo/external.txt"), "[not found: /repo/external.txt]")
            self.assertEqual(tree.cat("/repo/../secret.txt"), "[not found: /repo/../secret.txt]")

    def test_find_accepts_familiar_name_form_without_treating_name_as_glob(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "README.md").write_text("# Workspace\n", encoding="utf-8")
            source = root / "src" / "assistant"
            source.mkdir(parents=True)
            (source / "useAssistantConversation.ts").write_text("export default {};\n", encoding="utf-8")
            tree = ContextTree(root)
            tree.index_repo(max_files=1)
            parser = TreeCommandParser(tree)

            result = parser.parse_and_execute('find /repo -name "useAssistantConversation*" limit=20')

            self.assertTrue(result.ok)
            self.assertIn("repo/src/assistant/useAssistantConversation.ts", result.output)

    def test_find_symbol_scans_nested_repo_files_beyond_index_cap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "README.md").write_text("# Workspace\n", encoding="utf-8")
            component = root / "src" / "components" / "VariationsPanel.tsx"
            component.parent.mkdir(parents=True)
            component.write_text(
                "export function VariationsPanel() { return null; }\n",
                encoding="utf-8",
            )
            tree = ContextTree(root)
            tree.index_repo(max_files=1)
            parser = TreeCommandParser(tree)

            result = parser.parse_and_execute("find-symbol /repo VariationsPanel")

            self.assertTrue(result.ok)
            self.assertIn('"name": "VariationsPanel"', result.output)
            self.assertIn('"path": "/repo/src/components/VariationsPanel.tsx"', result.output)


class IssueListCommandTests(unittest.TestCase):
    def _parse_git(self, command: str) -> CommandResult:
        parser = TreeCommandParser(ContextTree(Path.cwd()))
        return parser.parse_and_execute(command)

    def test_beta_git_read_commands_preserve_supported_options(self):
        branch = self._parse_git("git branch -vv")
        remote = self._parse_git("git remote -v")
        diff = self._parse_git("git diff --staged --stat -- src/app.ts")
        log = self._parse_git("git log --oneline -12 origin/dev..HEAD -- src/app.ts")
        upstream_log = self._parse_git("git log @{u}..HEAD")

        self.assertEqual(branch.tool_action, {"type": "git_branch", "mode": "verbose"})
        self.assertEqual(remote.tool_action, {"type": "git_remote", "mode": "verbose"})
        self.assertEqual(
            diff.tool_action,
            {
                "type": "git_diff",
                "path": "src/app.ts",
                "staged": True,
                "stat": True,
                "name_only": False,
            },
        )
        self.assertEqual(
            log.tool_action,
            {"type": "git_log", "limit": 12, "revision": "origin/dev..HEAD", "path": "src/app.ts"},
        )
        self.assertEqual(
            upstream_log.tool_action,
            {"type": "git_log", "limit": 5, "revision": "@{u}..HEAD", "path": ""},
        )

    def test_beta_git_log_rejects_unsafe_or_multiple_revision_selectors(self):
        injected = self._parse_git("git log --exec=touch_bad origin/dev..HEAD")
        multiple = self._parse_git("git log origin/dev..HEAD main..HEAD")
        malformed = self._parse_git("git log origin/dev....HEAD")

        self.assertFalse(injected.ok)
        self.assertFalse(multiple.ok)
        self.assertFalse(malformed.ok)

    def test_beta_git_mutations_are_typed_and_path_bounded(self):
        restore = self._parse_git("git restore --staged -- src/a.ts src/b.ts")
        remove = self._parse_git("git rm src/old.ts")
        move = self._parse_git("git mv src/old.ts src/new.ts")
        commit = self._parse_git('git commit -m "fix: preserve beta state"')

        self.assertEqual(
            restore.tool_action,
            {"type": "git_restore", "paths": ["src/a.ts", "src/b.ts"], "staged": True},
        )
        self.assertEqual(remove.tool_action, {"type": "git_rm", "paths": ["src/old.ts"]})
        self.assertEqual(
            move.tool_action,
            {"type": "git_mv", "source": "src/old.ts", "destination": "src/new.ts"},
        )
        self.assertEqual(commit.tool_action, {"type": "git_commit", "message": "fix: preserve beta state"})

    def test_beta_git_push_supports_upstream_without_force_escape_hatches(self):
        push = self._parse_git("git push -u origin dev")
        forced = self._parse_git("git push --force-with-lease origin dev")
        deletion = self._parse_git("git push origin :dev")

        self.assertEqual(
            push.tool_action,
            {"type": "git_push", "remote": "origin", "branch": "dev", "set_upstream": True},
        )
        self.assertFalse(forced.ok)
        self.assertIn("not available", forced.output)
        self.assertFalse(deletion.ok)
        self.assertIn("not available", deletion.output)

    def test_beta_git_rejects_broad_paths_and_history_rewriting(self):
        broad_add = self._parse_git("git add .")
        glob_add = self._parse_git("git add 'src/*.ts'")
        reset = self._parse_git("git reset --hard")
        checkout = self._parse_git("git checkout -- src/app.ts")

        self.assertFalse(broad_add.ok)
        self.assertFalse(glob_add.ok)
        self.assertFalse(reset.ok)
        self.assertFalse(checkout.ok)

    def test_parse_strategy_accepts_dotted_sublabels_as_unique_steps(self):
        plan = parse_strategy(
            "\n".join([
                "s1.a: cat /repo/a.py",
                "s1.b: cat /repo/b.py",
                "s1.a, s1.b -> s2: fact demo/summary done",
            ])
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertIn("s1.a", plan.steps)
        self.assertIn("s1.b", plan.steps)
        self.assertEqual(plan.steps["s2"].depends_on, ["s1.a", "s1.b"])
        self.assertEqual(plan.execution_order[0], ["s1.a", "s1.b"])

    def test_parse_strategy_still_rejects_duplicate_dotted_sublabel(self):
        plan = parse_strategy(
            "\n".join([
                "s1.a: cat /repo/a.py",
                "s1.a: cat /repo/b.py",
            ])
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertIn("error", plan.steps)
        self.assertIn("duplicate label `s1.a`", plan.steps["error"].results[0].output)

    def test_extract_commands_ignores_bare_heredoc_terminator(self):
        self.assertEqual(
            extract_commands(">>th: done\n>>pl: finish\n>>>"),
            ">>th: done\n>>pl: finish",
        )

    def test_extract_commands_recovers_numbered_and_markdown_wrapped_commands(self):
        self.assertEqual(
            extract_commands(
                "\n".join(
                    [
                        "1. >>th: inspect the file",
                        "2. >>pl: read the target",
                        "3. `cat /repo/example.py`",
                    ]
                )
            ),
            ">>th: inspect the file\n>>pl: read the target\ncat /repo/example.py",
        )

    def test_extract_commands_recovers_explicit_command_labels(self):
        self.assertEqual(
            extract_commands("I will inspect the file.\nCommand: repo-map /repo topic=\"runtime\" limit=20"),
            'repo-map /repo topic="runtime" limit=20',
        )

    def test_extract_commands_strips_prose_attached_before_first_annotation(self):
        self.assertEqual(
            extract_commands(
                "Clearing stale errors first.>>th: inspect the current file\n"
                ">>pl: read the target\n"
                "cat /repo/example.py"
            ),
            ">>th: inspect the current file\n>>pl: read the target\ncat /repo/example.py",
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

    def test_issue_commands_reject_cross_namespace_ids(self):
        ledger = IssueFactLedger.empty()
        durable = ledger.create_issue(
            request_summary="Closed durable issue",
            plan_summary="Closed durable issue",
            activate=True,
        )
        ledger.close_issue(durable.issue_id, note="done")
        payload = ledger.planner_payload(path="/repo/repo_facts.md")
        tree = ContextTree(Path.cwd())
        run_issue_id = "run-ts1005-deadbeef01"
        tree.set_fact(tree.LOG_ISSUES_ROOT, run_issue_id, "status", "open")
        tree.set_fact(tree.LOG_ISSUES_ROOT, run_issue_id, "summary", "Open run diagnostic")
        parser = TreeCommandParser(tree, get_issue_state=lambda: payload)

        durable_result = parser.parse_and_execute(f"show-issue {durable.issue_id}")
        run_result = parser.parse_and_execute(f"show-run-issue {run_issue_id}")
        wrong_run_command = parser.parse_and_execute(f"show-run-issue {durable.issue_id}")
        wrong_durable_command = parser.parse_and_execute(f"show-issue {run_issue_id}")

        self.assertIn(f"Issue {durable.issue_id} [closed]", durable_result.output)
        self.assertIn(f"Run Diagnostic {run_issue_id} [open]", run_result.output)
        self.assertIn("Namespace mismatch", wrong_run_command.output)
        self.assertIn("Namespace mismatch", wrong_durable_command.output)

    def test_run_diagnostic_ids_are_stable_when_diagnostic_order_changes(self):
        tree = ContextTree(Path.cwd())
        original = "src/z.ts(20,4): error TS1005: ';' expected."

        first = tree.ingest_diagnostic_content(original, source_path="[run-check:typecheck]")
        second = tree.ingest_diagnostic_content(
            "src/a.ts(1,1): error TS2304: Cannot find name 'missing'.\n" + original,
            source_path="[run-check:typecheck]",
        )

        original_first_id = first[0]["id"]
        original_second_id = next(issue["id"] for issue in second if issue["code"] == "TS1005")
        self.assertEqual(original_first_id, original_second_id)
        self.assertTrue(original_first_id.startswith("run-ts1005-"))
        self.assertEqual(next(issue["namespace"] for issue in second if issue["code"] == "TS1005"), "run")

    def test_list_run_issues_excludes_durable_planner_issues(self):
        ledger = IssueFactLedger.empty()
        durable = ledger.create_issue(request_summary="Durable task", activate=True)
        tree = ContextTree(Path.cwd())
        tree.set_fact(tree.LOG_ISSUES_ROOT, "run-ts1005-deadbeef01", "status", "open")
        tree.set_fact(tree.LOG_ISSUES_ROOT, "run-ts1005-deadbeef01", "summary", "Run diagnostic")
        parser = TreeCommandParser(tree, get_issue_state=lambda: ledger.planner_payload(path="/repo/repo_facts.md"))

        result = parser.parse_and_execute("list-run-issues")

        self.assertIn("run-ts1005-deadbeef01", result.output)
        self.assertNotIn(durable.issue_id, result.output)
        self.assertNotIn("Durable issues from repo_facts", result.output)

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
