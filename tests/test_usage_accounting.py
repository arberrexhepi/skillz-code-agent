from __future__ import annotations

from types import MethodType
import unittest

from issue_facts import IssueFactLedger
from live_test_loop import TreeLoopPlannerWorker


class FakeUsageModel:
    def __init__(self, usage):
        self.usage = dict(usage)
        self.retry = {}

    def get_last_metrics(self):
        return {"usage": dict(self.usage), "retry": dict(self.retry)}


class UsageAccountingTests(unittest.TestCase):
    def _worker(self) -> TreeLoopPlannerWorker:
        worker = TreeLoopPlannerWorker.__new__(TreeLoopPlannerWorker)
        worker.model = FakeUsageModel(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_tokens": 75,
                "reasoning_tokens": 5,
            }
        )
        worker.provider = "openai"
        worker.model_name = "gpt-test"
        worker.issue_ledger = IssueFactLedger.empty()
        worker.issue_ledger.ensure_issue_open(request_summary="Reduce beta token cost", plan_summary="Cache transcript")
        worker.active_issue_id = worker.issue_ledger.active_issue_id
        worker._session_usage_totals = worker._empty_usage_totals()
        worker._active_run_usage_totals = worker._empty_usage_totals()
        worker._issue_usage_totals = {}
        worker._recent_model_turn_usage = []
        worker._observability_metrics = {}
        worker._llm_activity = {}
        worker.on_step_callback = None
        worker._reload_repo_facts = MethodType(lambda self: None, worker)
        return worker

    def test_model_finish_accumulates_current_issue_and_session_usage(self):
        worker = self._worker()

        worker._observe_model_event({"event": "model_call_finish", "turn": 1, "elapsed_s": 1.25, "output_chars": 30})
        worker.model.usage = {
            "input_tokens": 200,
            "output_tokens": 40,
            "cached_tokens": 160,
            "reasoning_tokens": 7,
        }
        worker._observe_model_event({"event": "model_call_finish", "turn": 2, "elapsed_s": 2.5, "output_chars": 60})

        accounting = worker.usage_accounting_state()
        session = accounting["session"]
        issue = accounting["current_issue"]["totals"]
        self.assertEqual(session["model_calls"], 2)
        self.assertEqual(session["input_tokens"], 300)
        self.assertEqual(session["output_tokens"], 60)
        self.assertEqual(session["cached_tokens"], 235)
        self.assertEqual(session["reasoning_tokens"], 12)
        self.assertEqual(issue, session)
        self.assertEqual(accounting["current_run"], session)
        self.assertEqual(len(accounting["issues"]), 1)
        self.assertEqual(accounting["issues"][0]["totals"], session)
        self.assertEqual(len(accounting["recent_turns"]), 2)
        self.assertEqual(accounting["recent_turns"][-1]["turn"], 2)

    def test_recovered_errors_are_kept_outside_rolling_recent_turns(self):
        worker = self._worker()
        worker.model.retry = {
            "attempts": 2,
            "retry_delays_s": [1.458],
            "fresh_context_retry": False,
            "errors": [
                {
                    "attempt": 1,
                    "status_code": 500,
                    "error_code": "server_error",
                    "message": "internal server error",
                    "request_id": "meta-request-1",
                    "exception_type": "InternalServerError",
                }
            ],
        }
        worker._observability_metrics = {"recovered_model_errors": []}
        blocks = []
        worker._append_observability_block = blocks.append
        worker._write_observability_snapshot = lambda **_kwargs: None

        worker._observe_model_event(
            {"event": "model_call_finish", "turn": 18, "elapsed_s": 2.5, "output_chars": 100}
        )

        recoveries = worker._observability_metrics["recovered_model_errors"]
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0]["turn"], 18)
        self.assertEqual(recoveries[0]["errors"][0]["status_code"], 500)
        self.assertEqual(recoveries[0]["errors"][0]["request_id"], "meta-request-1")
        self.assertIn("model call recovered", blocks[0])


if __name__ == "__main__":
    unittest.main()
