# Skills

Markdown-backed Playground OS skills live in this directory.

Format:

```md
---
name: example_skill
description: One-line description shown in skill discovery
args_schema: {"path": "str"}
tags: ["testing", "regression"]
category: testing
priority: 50
---
# Example Skill

Body content returned when the agent runs `skill example_skill`.
```

Notes:

- `name` and `description` are required.
- `args_schema` is optional and should be JSON on one line when present.
- `tags` is optional and should be a JSON-style list of short strings.
- `category` is optional and defaults to `general`.
- `priority` is optional and defaults to `0`.
- The Markdown body becomes the cached payload returned by the skill.
- Bundled skills in this folder are loaded automatically by both the stable runtime and the beta TreeLoop runtime.
- Workspace-local skills under `<target-repo>/skills/*.md` are also loaded and can override bundled names.