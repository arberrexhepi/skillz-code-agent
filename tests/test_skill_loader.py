from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from skill_loader import load_markdown_skills_from_dir


class MarkdownSkillLoaderTests(unittest.TestCase):
    def test_load_markdown_skills_from_dir_parses_front_matter_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir)
            (skill_dir / "testing_example.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: testing_example
                    description: Example testing skill
                    args_schema: {"path": "str"}
                    tags: ["testing", "example"]
                    category: testing
                    priority: 70
                    ---
                    # Example

                    Use targeted tests first.
                    """
                ),
                encoding="utf-8",
            )

            skills = load_markdown_skills_from_dir(skill_dir)

            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0].name, "testing_example")
            self.assertEqual(skills[0].description, "Example testing skill")
            self.assertEqual(skills[0].args_schema, {"path": "str"})
            self.assertEqual(skills[0].tags, ["testing", "example"])
            self.assertEqual(skills[0].category, "testing")
            self.assertEqual(skills[0].priority, 70)
            self.assertIn("Use targeted tests first.", skills[0].cache)

    def test_load_markdown_skills_from_dir_parses_multiline_modes_and_renders_selected_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir)
            (skill_dir / "contract_skill.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: contract_skill
                    description: Example contract skill
                    modes:
                      - fast
                      - deep
                    ---
                    >[global: shared]
                      invariants:
                        - Keep it deterministic
                    [/shared]<

                    [fast]
                    >[ref: shared]
                    Fast payload.
                    [/fast]

                    [deep]
                    >[ref: shared]
                    Deep payload.
                    [/deep]
                    """
                ),
                encoding="utf-8",
            )

            skills = load_markdown_skills_from_dir(skill_dir)

            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0].modes, ["fast", "deep"])
            rendered = skills[0].render(mode="deep")
            self.assertIn("mode: deep", rendered)
            self.assertIn("Keep it deterministic", rendered)
            self.assertIn("Deep payload.", rendered)
            self.assertNotIn("Fast payload.", rendered)

    def test_load_markdown_skills_from_dir_skips_readme_and_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir)
            (skill_dir / "README.md").write_text("# Skills\n", encoding="utf-8")
            (skill_dir / "broken.md").write_text("# Missing front matter\n", encoding="utf-8")

            skills = load_markdown_skills_from_dir(skill_dir)

            self.assertEqual(skills, [])