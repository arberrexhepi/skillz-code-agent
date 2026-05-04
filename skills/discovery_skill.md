---
name: codebase-discovery
description: Deterministic discovery layer for locating canonical implementations, symbols, relationships, and safe edit surfaces within a codebase prior to diagnostics or mutation.
modes:
  - fast
  - standard
  - deep
---

>[global: reasoning_rules]
  invariants:
    - Never treat search hits as truth; always resolve definition vs reference
    - Prefer smallest sufficient context (symbol > range > file)
    - Identify canonical ownership before proposing mutation targets
    - Expand context only until sufficient confidence is reached
  failure_modes:
    - Acting on references instead of definitions
    - Selecting non-canonical implementation
[/reasoning_rules]<

>[global: data_quality]
  invariants:
    - Outputs must contain real, normalized paths
    - Canonical candidates must be explicitly labeled
    - Edit targets must be subset of discovered candidates
    - Notes must include ambiguity flags when present
  failure_modes:
    - Returning vague or non-actionable paths
    - Missing ambiguity declaration
[/data_quality]<

---

[fast]

>[ref: reasoning_rules]
>[ref: data_quality]

>[ Skill config ]
  intent: "Quickly identify candidate files and symbols related to a topic, symbol, or file with minimal context expansion."
  input_schema:
    topic:  { type: string, required: false }
    symbol: { type: string, required: false }
    file:   { type: string, required: false }
  output_schema:
    candidates: { type: array }
    symbols:    { type: array }
    notes:      { type: array }
  invariants:
    - Use only lightweight discovery (list_files, find_files, search_in_files)
    - Avoid deep tracing or full file reads
    - Return quickly with best-effort candidates
  allowed_variance:
    completeness: true  — may return partial candidate sets
    canonical_resolution: false — do not attempt deep canonical resolution
  failure_modes:
    - Candidates too broad to act on
    - Relevant file not surfaced
[/Skill config]<

>>

Use fast discovery when speed matters more than completeness.

1. If symbol provided:
  - find_symbol_definitions(symbol_name="...", path=".") when you already expect a real definition
  - otherwise search_in_files(query="...", path=".", literal=true)
   - collect candidate files and symbol mentions

2. If topic provided:
  - semantic_search(intent="...", path=".") or search_in_files(query="...", path=".")
   - extract likely files

3. If file provided:
  - return the normalized file path directly as a candidate

4. Lightweight owned commands in this mode:
  - list_files(path=".", recursive=true, max_depth=N)
  - find_files(path=".", glob="pattern")
  - search_in_files(path=".", query="...")

5. Return:
   - candidate file paths
   - any identified symbol names
   - notes on ambiguity or gaps

<<

[/fast]

---

[standard]

>[ref: reasoning_rules]
>[ref: data_quality]

>[ Skill config ]
  intent: "Identify canonical candidates, edit targets, related files, and symbols with grounded context suitable for safe mutation."
  input_schema:
    topic:  { type: string, required: false }
    symbol: { type: string, required: false }
    file:   { type: string, required: false }
  output_schema:
    canonical_candidates: { type: array }
    likely_edit_targets:  { type: array }
    related_files:        { type: array }
    related_tests:        { type: array }
    symbols:
      { type: array }
    notes: { type: array }
  invariants:
    - Must distinguish definition vs reference
    - Must identify at least one canonical candidate when possible
    - Must include related files for structural awareness
    - Prefer symbol-level reads over full-file reads
  allowed_variance:
    canonical_candidates: true  — multiple allowed if ambiguity exists
    dependency_depth: false — limited to shallow relationships
  failure_modes:
    - Editing reference instead of definition
    - Missing related files or tests
    - Misidentifying canonical implementation
[/Skill config]<

>>

Use standard discovery before most mutations.

1. If symbol provided:
  - find_symbol_definitions(symbol_name="...", path=".")
  - read_symbol(path="...", symbol_name="...", symbol_kind="class|function|method|variable")
  - find_symbol_references(symbol_name="...", path=".")

2. If file provided:
  - outline_file(path="...")
  - read_symbol(...) when a concrete symbol is known; otherwise use read_file(path="...", start_line=..., end_line=...)
  - find_related_files(path="...")
  - find_related_tests(path="...") when mutation risk touches behavior

3. If topic provided:
  - semantic_search(intent="...", path=".")
  - search_in_files(query="...", path=".")

4. Always:
   - identify canonical_candidates
   - identify likely_edit_targets (subset of canonical)
   - gather related_files and tests

5. Return structured result with ambiguity notes if needed.

<<

[/standard]

---

[deep]

>[ref: reasoning_rules]
>[ref: data_quality]

>[ Skill config ]
  intent: "Perform full discovery including dependency tracing, config relationships, and impact surface analysis for high-risk or structural changes."
  input_schema:
    topic:  { type: string, required: false }
    symbol: { type: string, required: false }
    file:   { type: string, required: false }
  output_schema:
    canonical_candidates: { type: array }
    likely_edit_targets:  { type: array }
    related_files:        { type: array }
    related_tests:        { type: array }
    related_configs:      { type: array }
    dependency_edges:
      { type: array }
    symbols:
      { type: array }
    recommended_read_order: { type: array }
    notes: { type: array }
  invariants:
    - Must resolve canonical implementation or explicitly mark ambiguity
    - Must trace dependencies (imports/imported_by)
    - Must identify full impact surface
    - Must include configs and tests where relevant
    - Must propose minimal safe edit surface
  allowed_variance:
    dependency_depth: true  — may expand beyond depth 1 if required
    edit_strategy_options: true — may include multiple safe strategies
  failure_modes:
    - Over-expansion of context
    - False canonical resolution
    - Missing indirect dependencies
[/Skill config]<

>>

Use deep discovery for structural or high-risk changes.

1. Perform all standard steps

2. Additionally:
  - trace_dependencies(path="...", direction="both", depth=2)
  - find_related_configs(path="...")
  - expand related_files and tests with `find_related_files(path="...")` and `find_related_tests(path="...")`
   - identify dependency_edges

3. When the topic is still broad after standard discovery:
  - use investigate(topic="...", path=".", mode="standard"|"deep") to get a pre-aggregated discovery view

4. Build:
   - recommended_read_order (from entry → canonical → dependents)
   - minimal safe edit surface

5. Explicitly mark:
   - ambiguity
   - multiple valid canonical candidates if present

<<

[/deep]