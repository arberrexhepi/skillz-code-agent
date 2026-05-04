from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class MarkdownSkill:
    name: str
    description: str
    args_schema: Dict[str, Any]
    tags: List[str]
    category: str
    priority: int
    cache: str
    source_path: Path
    modes: List[str] = field(default_factory=list)

    def render(self, **kwargs: Any) -> str:
        raw_mode = kwargs.get("mode")
        mode = str(raw_mode or "").strip()
        if not mode:
            return self.cache

        normalized_mode = mode.lower()
        if self.modes and normalized_mode not in {item.lower() for item in self.modes}:
            available = ", ".join(self.modes)
            return f"Error: unknown mode '{mode}' for skill '{self.name}'. Available modes: {available}"

        rendered = _render_mode_payload(self.cache, normalized_mode)
        if rendered is None:
            return self.cache
        header = [f"# {self.name}", f"description: {self.description}", f"mode: {normalized_mode}"]
        if self.modes:
            header.append("available_modes: " + ", ".join(self.modes))
        return "\n".join(header) + "\n\n" + rendered


def load_markdown_skills_from_dir(skill_dir: Path) -> List[MarkdownSkill]:
    directory = Path(skill_dir)
    if not directory.exists() or not directory.is_dir():
        return []

    skills: List[MarkdownSkill] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        try:
            skills.append(_load_markdown_skill(path))
        except ValueError:
            continue
    return skills


def _load_markdown_skill(path: Path) -> MarkdownSkill:
    text = path.read_text(encoding="utf-8")
    metadata, body = _split_front_matter(text, source_path=path)

    name = str(metadata.get("name", "") or "").strip()
    description = str(metadata.get("description", "") or "").strip()
    if not name:
        raise ValueError(f"Skill file {path} is missing a name")
    if not description:
        raise ValueError(f"Skill file {path} is missing a description")

    raw_args_schema = metadata.get("args_schema", metadata.get("args", {}))
    args_schema = raw_args_schema if isinstance(raw_args_schema, dict) else {}
    raw_tags = metadata.get("tags", [])
    tags = [str(item).strip() for item in raw_tags if str(item).strip()] if isinstance(raw_tags, list) else []
    category = str(metadata.get("category", "general") or "general").strip() or "general"
    raw_priority = metadata.get("priority", 0)
    priority = int(raw_priority) if isinstance(raw_priority, int) else 0
    raw_modes = metadata.get("modes", [])
    modes = [str(item).strip() for item in raw_modes if str(item).strip()] if isinstance(raw_modes, list) else []
    cache = body.strip()
    if not cache:
        raise ValueError(f"Skill file {path} has no body content")

    return MarkdownSkill(
        name=name,
        description=description,
        args_schema=args_schema,
        tags=tags,
        category=category,
        priority=priority,
        modes=modes,
        cache=cache,
        source_path=path,
    )


def _split_front_matter(text: str, *, source_path: Path) -> tuple[Dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError(f"Skill file {source_path} is missing front matter")

    lines = text.splitlines()
    closing_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        raise ValueError(f"Skill file {source_path} has unterminated front matter")

    metadata_lines = lines[1:closing_index]
    body = "\n".join(lines[closing_index + 1 :]).lstrip("\n")
    metadata: Dict[str, Any] = {}
    index = 0
    while index < len(metadata_lines):
        raw_line = metadata_lines[index]
        line = raw_line.strip()
        if not line or line.startswith("#"):
            index += 1
            continue
        if ":" not in line:
            raise ValueError(f"Invalid front matter line in {source_path}: {raw_line}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if value:
            metadata[key] = _parse_front_matter_value(value)
            index += 1
            continue

        nested_lines: List[str] = []
        index += 1
        while index < len(metadata_lines):
            candidate = metadata_lines[index]
            stripped = candidate.strip()
            if not stripped:
                index += 1
                continue
            if not candidate.startswith((" ", "\t")):
                break
            nested_lines.append(candidate)
            index += 1
        metadata[key] = _parse_indented_front_matter_block(nested_lines)
    return metadata, body


def _parse_front_matter_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if value.startswith("{") or value.startswith("["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        try:
            return ast.literal_eval(value)
        except Exception:
            return value[1:-1]
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    return value


def _parse_indented_front_matter_block(lines: List[str]) -> Any:
    stripped_lines = [line.strip() for line in lines if line.strip()]
    if not stripped_lines:
        return []
    if all(line.startswith("- ") for line in stripped_lines):
        return [_parse_front_matter_value(line[2:].strip()) for line in stripped_lines]

    mapping: Dict[str, Any] = {}
    for line in stripped_lines:
        if ":" not in line:
            raise ValueError(f"Invalid nested front matter line: {line}")
        key, raw_value = line.split(":", 1)
        mapping[key.strip()] = _parse_front_matter_value(raw_value.strip())
    return mapping


_GLOBAL_RE = re.compile(r">\[global:\s*([^\]]+)\](.*?)\[/([^\]]+)\]<", flags=re.DOTALL)
_MODE_RE_TEMPLATE = r"\[{mode}\](.*?)\[/{mode}\]"
_REF_RE = re.compile(r">\[ref:\s*([^\]]+)\]\s*")


def _render_mode_payload(body: str, mode: str) -> Optional[str]:
    mode_match = re.search(_MODE_RE_TEMPLATE.format(mode=re.escape(mode)), body, flags=re.DOTALL)
    if mode_match is None:
        return None

    globals_map: Dict[str, str] = {}
    for match in _GLOBAL_RE.finditer(body):
        key = str(match.group(1) or "").strip()
        closing = str(match.group(3) or "").strip()
        if key and key == closing:
            globals_map[key] = str(match.group(2) or "").strip()

    mode_body = str(mode_match.group(1) or "").strip()
    referenced_keys = [str(match.group(1) or "").strip() for match in _REF_RE.finditer(mode_body) if str(match.group(1) or "").strip()]
    mode_body = _REF_RE.sub("", mode_body).strip()

    parts: List[str] = []
    seen: set[str] = set()
    for key in referenced_keys:
        if key in seen:
            continue
        seen.add(key)
        payload = globals_map.get(key)
        if payload:
            parts.append(f"[global:{key}]\n{payload}\n[/global:{key}]")
    parts.append(mode_body)
    return "\n\n".join(part for part in parts if part.strip())