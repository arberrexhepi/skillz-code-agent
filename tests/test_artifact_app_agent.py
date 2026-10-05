import unittest

from artifact_app_agent import structured


class ArtifactAppAgentTests(unittest.TestCase):
    def test_structured_accepts_json_and_fenced_tool_calls(self) -> None:
        self.assertEqual(structured('{"type":"answer","content":"hello"}')["content"], "hello")
        action = structured('```json\n{"type":"tool","name":"api_tasks","input":{"state":"open"}}\n```')
        self.assertEqual(action["name"], "api_tasks")

    def test_unstructured_model_output_remains_a_user_facing_answer(self) -> None:
        self.assertEqual(structured("Plain answer"), {"type": "answer", "content": "Plain answer"})


if __name__ == "__main__":
    unittest.main()
