from __future__ import annotations

import unittest

from live_test_loop import TreeLoopPlannerWorker


class BetaGitExecutorTests(unittest.TestCase):
    def _worker(self, task: str = ""):
        worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
        worker._current_task = task
        calls = []

        def run_git(args, *, timeout=60):
            calls.append((list(args), timeout))
            return 0, "ok", ""

        worker._run_git = run_git
        return worker, calls

    def test_verbose_branch_and_remote_commands_execute_exactly(self):
        worker, calls = self._worker()

        self.assertEqual(worker._exec_git_branch({"mode": "verbose"}), "ok")
        self.assertEqual(worker._exec_git_remote({"mode": "verbose"}), "ok")

        self.assertEqual(calls, [(["branch", "-vv"], 60), (["remote", "-v"], 60)])

    def test_log_revision_range_is_forwarded_before_path_separator(self):
        worker, calls = self._worker()

        self.assertEqual(
            worker._exec_git_log(
                {"limit": 12, "revision": "origin/dev..HEAD", "path": "src/app.ts"}
            ),
            "ok",
        )

        self.assertEqual(
            calls,
            [(["log", "--oneline", "-12", "origin/dev..HEAD", "--", "src/app.ts"], 60)],
        )

    def test_restore_and_remove_use_separator_and_explicit_paths(self):
        worker, calls = self._worker()

        worker._exec_git_restore({"paths": ["src/a.ts", "src/b.ts"], "staged": True})
        worker._exec_git_rm({"paths": ["src/old.ts"]})

        self.assertEqual(
            calls,
            [
                (["restore", "--staged", "--", "src/a.ts", "src/b.ts"], 60),
                (["rm", "--", "src/old.ts"], 60),
            ],
        )

    def test_push_requires_explicit_task_authorization(self):
        worker, calls = self._worker("Inspect the repository but do not push")

        message = worker._exec_git_push(
            {"remote": "origin", "branch": "dev", "set_upstream": True}
        )

        self.assertIn("does not explicitly authorize", message)
        self.assertEqual(calls, [])

    def test_authorized_push_and_remote_verification_have_bounded_forms(self):
        worker, calls = self._worker("Original request: git commit and push\nGoal: Push to remote and verify")

        push = worker._exec_git_push(
            {"remote": "origin", "branch": "dev", "set_upstream": True}
        )
        verify = worker._exec_git_ls_remote(
            {"remote": "origin", "ref": "refs/heads/dev"}
        )

        self.assertEqual(push, "ok")
        self.assertEqual(verify, "ok")
        self.assertEqual(
            calls,
            [
                (["push", "--set-upstream", "origin", "dev"], 120),
                (["ls-remote", "origin", "refs/heads/dev"], 120),
            ],
        )


if __name__ == "__main__":
    unittest.main()
