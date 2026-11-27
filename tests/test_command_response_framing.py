import pytest

from command_blocks import BEGIN_COMMANDS, END_COMMANDS
from model_output_progress import ModelOutputObserver, ModelOutputProgress
from mutation_text import PARSE_ERROR_PREFIX
from tree_loop import TreeLoop, extract_commands


@pytest.mark.parametrize("invalid", [
    "cat /repo/note.txt",
    f"{BEGIN_COMMANDS}\ncat /repo/note.txt",
    f"{BEGIN_COMMANDS}\nwrite /repo/note.txt <<<\n{END_COMMANDS}",
    f"{BEGIN_COMMANDS}\nfinish Done\n{END_COMMANDS}\n{BEGIN_COMMANDS}\nfinish Again\n{END_COMMANDS}",
])
def test_framed_extraction_rejects_missing_incomplete_or_duplicate_frames(invalid):
    assert extract_commands(invalid, require_frame=True).startswith(PARSE_ERROR_PREFIX)


def test_preview_and_dispatch_share_the_frame_and_preserve_heredoc_markers():
    body = f"write /repo/note.txt <<<\n{END_COMMANDS}\n{BEGIN_COMMANDS}\ncat /repo/literal.txt\n>>>\nfinish Done"
    reply = (
        f"An example follows.\n```bash\n{BEGIN_COMMANDS}\ncat /repo/example.txt\n{END_COMMANDS}\n```\n"
        f"{BEGIN_COMMANDS}\n{body}\n{END_COMMANDS}\ncat /repo/after.txt"
    )
    entries = []
    progress = ModelOutputProgress(ModelOutputObserver(
        lambda _count, draft: entries.extend(draft), preview_strategy=True, require_command_frame=True,
    ))
    for offset in range(0, len(reply), 7):
        progress.add(reply[offset:offset + 7])
    progress.finish()

    assert entries == ["write", "finish"]
    assert extract_commands(reply, require_frame=True) == body


def test_missing_close_retries_without_dispatching_any_of_the_draft(tmp_path):
    class Model:
        command_response_framing = True
        calls = 0

        def complete_messages(self, system, messages):
            self.calls += 1
            assert "Keep provider reasoning summaries in their own channel" in system
            assert f"{BEGIN_COMMANDS}\n>>th:" in system
            if self.calls == 1:
                return f"{BEGIN_COMMANDS}\nwrite /repo/note.txt incomplete"
            assert not (tmp_path / "note.txt").exists()
            assert f"Missing {END_COMMANDS}" in messages[-1]["content"]
            return (f"write /repo/preamble.txt ignored\n{BEGIN_COMMANDS}\nwrite /repo/note.txt complete\nfinish Done\n"
                    f"{END_COMMANDS}\nwrite /repo/trailer.txt ignored")

    model = Model()
    loop = TreeLoop(model=model, workspace_root=tmp_path, max_turns=1, verbose=False)
    result = loop.run("Write the note")

    assert model.calls == 2
    assert result.finished
    assert (tmp_path / "note.txt").read_text() == "complete"
    assert not (tmp_path / "preamble.txt").exists()
    assert not (tmp_path / "trailer.txt").exists()


def test_ambiguous_inline_patch_can_retry_as_a_literal_patch(tmp_path):
    class Model:
        command_response_framing = True
        calls = 0

        def complete_messages(self, system, messages):
            self.calls += 1
            assert "patch /repo/rules.txt --literal <<<" in system
            if self.calls == 1:
                commands = "s1: write /repo/ready.txt ready\ns1 -> s2: patch /repo/rules.txt A -> B -> C -> A -> D -> C"
            else:
                assert not (tmp_path / "ready.txt").exists()
                assert target.read_text() == "A -> B -> C\n"
                repair = messages[-1]["content"]
                assert "Malformed `patch` command" in repair
                assert "patch <path> --literal <<<" in repair
                assert "=== REPLACE ===" in repair
                commands = (
                    "s1: write /repo/ready.txt ready\ns1 -> s2: patch /repo/rules.txt --literal <<<\n"
                    "A -> B -> C\n=== REPLACE ===\nA -> D -> C\n>>>\ns2 -> s3: finish Updated the rules"
                )
            return f"{BEGIN_COMMANDS}\n{commands}\n{END_COMMANDS}"

    target = tmp_path / "rules.txt"
    target.write_text("A -> B -> C\n")
    model = Model()
    loop = TreeLoop(model=model, workspace_root=tmp_path, max_turns=1, verbose=False)
    loop._append_backend_diagnostics = lambda message, *args, **kwargs: message

    result = loop.run("Update the rules and write the ready marker")

    assert model.calls == 2
    assert result.finished
    assert target.read_text() == "A -> D -> C\n"
    assert (tmp_path / "ready.txt").read_text() == "ready"


@pytest.mark.parametrize("strategy, expected_reads", [
    ("s1: cat /repo/note.txt\ns1 -> s2: finish {summary}", ["First note"]),
    ("s1: cat /repo/note.txt, cat /repo/other.txt, finish {summary}", ["First note", "Second note"]),
])
def test_strategy_finish_preserves_summary_punctuation_without_a_format_retry(tmp_path, strategy, expected_reads):
    summary = "Added TimelineClip schema, SQLite persistence, and timeline routes. Plan diagram -> s99"

    class Model:
        command_response_framing = True
        calls = 0

        def complete_messages(self, system, messages):
            self.calls += 1
            commands = strategy.format(summary=summary)
            return f"{BEGIN_COMMANDS}\n{commands}\n{END_COMMANDS}"

    (tmp_path / "note.txt").write_text("First note")
    (tmp_path / "other.txt").write_text("Second note")
    model = Model()
    loop = TreeLoop(model=model, workspace_root=tmp_path, max_turns=1, verbose=False)

    result = loop.run("Report the completed work")

    assert model.calls == 1
    assert result.finished
    assert result.finish_message == f"[finish: {summary}]"
    reads = [step.output.partition("| ")[2] for step in result.turns[0].results if step.command_type == "read" and step.ok]
    assert reads == expected_reads
