import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from planner import GoalExecutionResult, PlannerAgent
from run_observability import archive_snapshot, clear_archives
from context_tree import ContextTree


class AttemptReportingTests(unittest.TestCase):
    def test_archived_traces_do_not_pollute_source_search_but_allow_explicit_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = archive_snapshot(root / "memory_observability.md", "archived_unique_marker")
            tree = ContextTree(root)
            tree.index_repo()
            self.assertEqual(tree.grep("/repo", "archived_unique_marker"), [])
            self.assertIn("archived_unique_marker", tree.cat("/repo/" + report.relative_to(root).as_posix()))

    def test_archive_preserves_each_attempt_and_deduplicates_identical_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp) / "memory_observability.md"
            live.write_text("First attempt: turn limit reached")
            first = archive_snapshot(live)
            self.assertEqual(archive_snapshot(live), first)
            live.write_text("Second attempt: running")
            second = archive_snapshot(live)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(), "First attempt: turn limit reached")
            clear_archives(live)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_failure_summary_contains_the_worker_stop_reason_and_validation(self):
        planner = PlannerAgent.__new__(PlannerAgent)
        planner.session = SimpleNamespace(last_next_steps=[])
        result = GoalExecutionResult(goal_id="goal-2", title="Composer", delegated_task="Implement composer",
                                     status="failed", final_message="Turn limit reached: 100 model turns in this attempt.",
                                     validation_summary="An edit batch is still open.")
        summary = planner._synthesize_final_summary(SimpleNamespace(), [result])
        self.assertIn(result.final_message, summary)
        self.assertIn(result.validation_summary, summary)
