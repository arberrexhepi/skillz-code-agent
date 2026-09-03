from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mutations import batch_mutate


class BatchMutationSchemaTests(unittest.TestCase):
    def test_batch_accepts_public_path_field_for_create_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            result = batch_mutate(
                [{"type": "create_file", "path": "src/new.py", "content": "value = 1\n"}],
                atomic=True,
                root=root,
            )

            self.assertTrue(result["ok"])
            self.assertEqual((root / "src/new.py").read_text(encoding="utf-8"), "value = 1\n")

    def test_batch_normalizes_path_and_all_for_text_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "sample.txt"
            target.write_text("old old\n", encoding="utf-8")

            result = batch_mutate(
                [
                    {
                        "type": "replace_snippet",
                        "path": "sample.txt",
                        "old_text": "old",
                        "new_text": "new",
                        "expected_occurrences": 2,
                        "all": True,
                    }
                ],
                atomic=True,
                root=root,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(target.read_text(encoding="utf-8"), "new new\n")


if __name__ == "__main__":
    unittest.main()
