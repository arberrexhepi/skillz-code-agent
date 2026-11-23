"""Stage ordered file edits and publish them only after syntax and version checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat
import tempfile

from symbol_ranges import typescript_syntax_errors
from .batch_state import FileState, operation_paths
from .common import safe_join


def anchored_range(lines: list[str], first: int, last: int, expected: list[str], *, allow_position: bool = True) -> tuple[int, int]:
    if allow_position and expected and lines[first - 1:last] == expected:
        return first, first + len(expected) - 1
    length = len(expected)
    if length:
        matches = [offset + 1 for offset, line in enumerate(lines) if line == expected[0] and lines[offset:offset + length] == expected]
        if len(matches) == 1:
            return matches[0], matches[0] + length - 1
    start = max(1, min(first, len(lines)) - 2)
    end = min(len(lines), max(start + 4, min(last + 2, start + 15)))
    context = "\n".join(f"{number} | {lines[number - 1]}" for number in range(start, end + 1))
    raise ValueError(f"Source for lines {first}-{last} no longer matches uniquely. Re-read the staged range or use an exact patch.\n{context}")


class StageConflict(ValueError):
    """The working tree changed after the batch captured its inputs."""


@dataclass(frozen=True)
class StagedState(FileState):
    mode: int = 0

    @classmethod
    def read(cls, path: Path) -> StagedState:
        state = FileState.read(path)
        return cls(state.kind, state.data, stat.S_IMODE(path.stat().st_mode) if state.kind == "file" else 0)


class StagedFiles:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._temp = tempfile.TemporaryDirectory(prefix="skillz-edit-")
        self.directory = Path(self._temp.name).resolve()
        self.originals: dict[str, StagedState] = {}
        self.aliases: dict[str, str] = {}

    def include(self, path: str) -> Path:
        target = safe_join(self.root, path)
        relative = target.relative_to(self.root).as_posix()
        self.aliases[Path(path).as_posix()] = relative
        staged = self.directory / relative
        if relative not in self.originals:
            initial = StagedState.read(target)
            if initial.kind not in {"file", "missing"}:
                raise ValueError(f"Expected a file at {relative}")
            self.originals[relative] = initial
            if initial.kind == "file":
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(initial.data)
                staged.chmod(initial.mode)
        return staged

    def lookup(self, path: str) -> Path | None:
        relative = self.aliases.get(path, path)
        return self.directory / relative if relative in self.originals else None

    def prepare_operations(self, operations: list[dict]) -> list[dict]:
        staged_paths = {path: self.include(path).relative_to(self.directory).as_posix()
                        for operation in operations for path in operation_paths(operation)}
        path_keys = {"path", "file_path", "old_path", "new_path", "source_path", "destination_path"}
        return [{key: staged_paths.get(value.strip(), value) if key in path_keys and isinstance(value, str) else value
                 for key, value in operation.items()} for operation in operations]

    def changes(self) -> dict[str, StagedState]:
        return {relative: state for relative, original in self.originals.items()
                if (state := StagedState.read(self.directory / relative)) != original}

    def syntax_errors(self, changes: dict[str, StagedState]) -> list[dict]:
        return [{"path": relative, **finding}
                for relative, state in changes.items() if state.kind == "file" and Path(relative).suffix.lower() == ".tsx"
                for finding in typescript_syntax_errors(self.root / relative, state.data.decode("utf-8"), root=self.root)]

    def publish(self, changes: dict[str, StagedState]) -> list[str]:
        # Check every input, including the source of a staged file copy.
        for name, relative in self.aliases.items():
            if safe_join(self.root, name) != self.root / relative:
                raise StageConflict(f"{name} changed outside the edit batch. Re-read it before retrying.")
        for relative, original in self.originals.items():
            self._check(relative, original)
        if any(state.kind not in {"file", "missing"} for state in changes.values()):
            raise ValueError("The edit batch attempted to replace a file with a directory.")
        published: dict[str, StagedState] = {}
        try:
            for relative, state in changes.items():
                self._check(relative, self.originals[relative])
                self._write(self.root / relative, state)
                published[relative] = state
            for relative, state in published.items():
                self._check(relative, state)
        except (OSError, ValueError) as exc:
            conflicts = []
            for relative, state in reversed(published.items()):
                try:
                    self._check(relative, state)
                    self._write(self.root / relative, self.originals[relative])
                except (OSError, ValueError):
                    conflicts.append(relative)
            if conflicts:
                raise ValueError(f"{exc} Could not roll back {', '.join(conflicts)}.") from exc
            raise
        return sorted(published)

    def _check(self, relative: str, expected: StagedState) -> None:
        target = safe_join(self.root, relative)
        if target != self.root / relative or StagedState.read(target) != expected:
            raise StageConflict(f"{relative} changed outside the edit batch. Re-read it before retrying.")

    @staticmethod
    def _write(target: Path, state: StagedState) -> None:
        if state.kind == "missing":
            target.unlink()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as writer:
            temporary = Path(writer.name)
            try:
                writer.write(state.data)
                writer.close()
                temporary.chmod(state.mode)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    def close(self) -> None:
        self._temp.cleanup()
