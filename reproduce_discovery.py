import json
import io
import tempfile
import unittest.mock as mock
from pathlib import Path
from live_test_loop import main
from tests.test_live_test_loop import SharedCursorFakeModelClient

def reproduce():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        Path(root, "main.py").write_text("print('hello')\n")
        offer_discovery = json.dumps(
            {
                "action": {
                    "type": "offer_discovery",
                    "reason": "Need repo discovery before planning.",
                    "prompt": "Choose discovery depth.",
                    "recommended_mode": "deep",
                }
            }
        )
        present_plan = json.dumps(
            {
                "action": {
                    "type": "present_plan",
                    "summary": "Use discovered context.",
                    "goals": [
                        {
                            "goal_id": "goal-1",
                            "title": "Inspect main",
                            "goal": "Read main.py and summarize it.",
                            "reason": "Need discovered entrypoint context.",
                        }
                    ],
                }
            }
        )
        stdin_payload = "\n".join(
            [
                json.dumps({"id": "1", "type": "initialize"}),
                json.dumps({"id": "2", "type": "submit", "text": "inspect the repo"}),
                json.dumps({"id": "3", "type": "planner_action", "action": "select_discovery_mode", "mode": "deep"}),
            ]
        ) + "\n"

        model = SharedCursorFakeModelClient([
            offer_discovery,
            "I'll check the Mechanics.md file and the grid implementation to provide accurate instructions.",
            "cat /repo/main.py\nfinish discovery complete",
            present_plan,
        ])
        with mock.patch("live_test_loop.create_model_client", return_value=model):
            with mock.patch("sys.stdin", io.StringIO(stdin_payload)):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    try:
                        exit_code = main(["--root", str(root), "--extension-bridge"])
                    except SystemExit as e:
                        exit_code = e.code

        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        discovery_progress = [line for line in lines if line.get("type") == "progress" and line.get("domain") == "discovery"]
        
        for p in discovery_progress:
            print(json.dumps(p))
        
        if lines:
            print(json.dumps(lines[-1]))

if __name__ == "__main__":
    reproduce()
