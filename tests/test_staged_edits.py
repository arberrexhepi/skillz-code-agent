from pathlib import Path
import shutil
from unittest.mock import patch

import pytest

from live_test_loop import TreeLoopPlannerWorker
from mutations import batch_mutate
from mutations.staging import anchored_range
from planner import PlannerAgent
from symbol_ranges import typescript_syntax_errors
from tests.test_live_test_loop import FakeModelClient


pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not (Path(__file__).parents[1] / "desktop/node_modules/typescript").is_dir(),
    reason="Local TypeScript parser required",
)


def test_shifted_range_is_reanchored_and_reads_see_the_staged_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
    source = tmp_path / "SceneTimeline.tsx"
    original = '// a\n// b\n// c\n// d\nexport const timeline = (\n  <div>\n    <span>Trim</span>\n  </div>\n);\n'
    source.write_text(original)
    worker = TreeLoopPlannerWorker(model=FakeModelClient([
        "read-line-range /repo/SceneTimeline.tsx 1-9",
        'batch start\nreplace-lines /repo/SceneTimeline.tsx:1-4 // a\n'
        'replace-lines /repo/SceneTimeline.tsx:6-6   <div aria-label="Timeline">\n'
        'read-line-range /repo/SceneTimeline.tsx 2-5',
        "batch end\nfinish Updated",
    ]), root=tmp_path, max_turns=3, checkpoint_interval=0, verbose=False)

    def check(path, text, *, root):
        assert source.read_text() == original
        return typescript_syntax_errors(path, text, root=root)

    with patch("mutations.staging.typescript_syntax_errors", side_effect=check) as syntax:
        result = worker.run_task("Update the timeline label")

    assert result.ok, result.final_message
    assert 're-anchored from 6-6' in worker.history[1].results[2].output
    assert '<div aria-label="Timeline">' in worker.history[1].results[3].output
    assert source.read_text() == original.replace('// b\n// c\n// d\n', '').replace('<div>', '<div aria-label="Timeline">')
    assert PlannerAgent._worker_mutation_paths(worker.history) == [source.name]
    syntax.assert_called_once()


def test_failed_tsx_preflight_keeps_the_staged_version_for_an_overlapping_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
    source = tmp_path / "App.tsx"
    original = 'export const View = (\n  <div>\n    <span />\n  </div>\n);\n'
    source.write_text(original)
    worker = TreeLoopPlannerWorker(model=FakeModelClient([
        'batch start\nreplace-lines /repo/App.tsx:2-2   <main>\nbatch end\nfinish Premature',
        'read-line-range /repo/App.tsx 2-4\nreplace-lines /repo/App.tsx:2-4 <<<\n'
        '  <section>\n    <span />\n  </section>\n>>>\nbatch end\nfinish Repaired',
    ]), root=tmp_path, max_turns=2, checkpoint_interval=0, verbose=False)

    def check(path, text, *, root):
        assert source.read_text() == original
        return typescript_syntax_errors(path, text, root=root)

    with patch("mutations.staging.typescript_syntax_errors", side_effect=check) as syntax:
        result = worker.run_task("Change the wrapper")

    assert result.ok, result.final_message
    assert "TS17002" in worker.history[0].results[2].output
    assert not worker.history[0].results[3].ok
    assert "<main>" in worker.history[1].results[0].output
    assert source.read_text() == original.replace('div', 'section')
    assert syntax.call_count == 2


def test_atomic_edit_batch_checks_final_tsx_and_keeps_all_files_intact_on_failure(tmp_path):
    source = tmp_path / "App.tsx"
    css = tmp_path / "style.css"
    original = 'export const View = <div>old</div>;\n'
    source.write_text(original)
    css.write_text("old\n")
    operations = [
        {"type": "replace_snippet", "path": css.name, "old_text": "old", "new_text": "new"},
        {"type": "replace_snippet", "path": source.name, "old_text": "<div>old", "new_text": "<main>old"},
    ]
    result = batch_mutate(operations, atomic=True, root=tmp_path)
    assert not result["ok"]
    assert result["operations"][0]["diagnostics"][0]["code"] == "TS17002"
    assert source.read_text() == original
    assert css.read_text() == "old\n"

    operations.append({"type": "replace_range", "path": source.name, "start_line": 1, "end_line": 1,
                       "expected_text": 'export const View = <main>old</div>;', "new_text": 'export const View = <main>new</main>;'})
    result = batch_mutate(operations, atomic=True, root=tmp_path)
    assert result["ok"], result
    assert source.read_text() == 'export const View = <main>new</main>;\n'
    assert css.read_text() == "new\n"


def test_reanchoring_rejects_an_ambiguous_moved_range():
    with pytest.raises(ValueError, match="no longer matches uniquely"):
        anchored_range(["shifted", "  <div>", "  <div>"], 1, 1, ["  <div>"])
    with pytest.raises(ValueError, match="no longer matches uniquely"):
        anchored_range(["shifted", "  <div>", "  <div>"], 2, 2, ["  <div>"], allow_position=False)


def test_external_edit_during_preflight_prevents_publication(tmp_path):
    source = tmp_path / "App.tsx"
    companion = tmp_path / "style.css"
    original = 'export const View = <div>old</div>;\n'
    source.write_text(original)
    companion.write_text("old\n")

    def check(path, text, *, root):
        assert source.read_text() == original
        companion.write_text("external edit\n")
        return typescript_syntax_errors(path, text, root=root)

    with patch("mutations.staging.typescript_syntax_errors", side_effect=check):
        result = batch_mutate([
            {"type": "replace_snippet", "path": source.name, "old_text": "old", "new_text": "new"},
            {"type": "replace_snippet", "path": companion.name, "old_text": "old", "new_text": "new"},
        ], atomic=True, root=tmp_path)

    assert not result["ok"]
    assert "changed outside the edit batch" in result["operations"][0]["diagnostics"][0]["message"]
    assert source.read_text() == original
    assert companion.read_text() == "external edit\n"
