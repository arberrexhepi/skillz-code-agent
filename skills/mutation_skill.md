---
name: codebase-mutation
description: Deterministic mutation layer for applying controlled, delta-aware changes to codebases through explicit mutation surfaces with applicability checks, drift detection, and post-write verification.
modes:
  - surgical_text
  - structural_code
  - filesystem
  - coordinated_batch
---

>[global: mutation_rules]
  invariants:
    - Always prefer the smallest mutation surface that fully satisfies the change
    - Never apply mutation without grounded target context
    - Every mutation must validate applicability before landing
    - Every mutation must detect drift before applying deltas
    - Every landed mutation must support post-write verification
    - Mutations must be explicit, auditable, and bounded in scope
  failure_modes:
    - Applying mutation to stale context
    - Using an overly broad mutation surface
    - Landing unverified changes
    - Changing stable and unrelated code, especially when not in scope of current goals. 
[/mutation_rules]<

>[global: safety_contract]
  invariants:
    - Mutations must preserve exact target identity through anchors, ranges, or symbol resolution
    - Idempotent states must be detected and reported without duplicate edits
    - Structural divergence must block landing until re-grounded
    - Policy-governed restrictions must be enforced before filesystem-changing operations
  failure_modes:
    - Duplicate inserts on retry
    - Mutation landing against shifted context
    - Unauthorized file creation, deletion, or rename
[/safety_contract]<

---

[surgical_text]

>[ref: mutation_rules]
>[ref: safety_contract]

>[ Skill config ]
  intent: "Apply narrow, text-level mutations to grounded ranges or anchored snippets with minimal edit surface."
  input_schema:
    operation: { type: string, required: true, note: "one of replace_range, replace_snippet, insert_before, insert_after, delete_range, delete_snippet, append_block, prepend_block" }
    path: { type: string, required: true, note: "target file path" }
    target: { type: object, required: true, note: "actual arguments depend on operation: start_line/end_line, old_text, text, or anchor_text" }
    content: { type: string, required: false, note: "use new_text for inserts/replacements when calling the owned commands" }
  output_schema:
    applied: { type: boolean, note: "whether the mutation was landed" }
    mutation_type: { type: string, note: "the specific mutation operation used" }
    file_path: { type: string, note: "normalized target path returned by the mutation result" }
    changed_line_count: { type: integer, note: "number of lines changed" }
    verification: { type: object, note: "post-write verification result" }
    notes: { type: array, note: "drift, idempotence, or scope notes" }
  invariants:
    - Must use only text-local mutations in this mode
    - Must require grounded range, snippet, or anchor context
    - Must fail on ambiguous snippet or anchor matches unless explicitly allowed
    - Must report already-applied states instead of duplicating content
    - Must not rewrite entire symbols or files in this mode
  allowed_variance:
    anchor_strategy: true — may choose range, snippet, or anchor targeting based on stability
    idempotence_detection: true — may return applied false when desired state already exists
    structural_rewrites: false — do not perform symbol-level or file-level mutation in this mode
  failure_modes:
    - Ambiguous anchor or snippet target
    - Range drift exceeds safe threshold
    - Duplicate insertion on retry
    - Local text mutation insufficient for requested structural change
[/Skill config]<

>>

Use this mode for small, local, bounded edits.

Valid operations include:
- replace_range
- replace_snippet
- insert_before
- insert_after
- delete_range
- delete_snippet
- append_block
- prepend_block

Owned command shapes:
- `replace_range(path, start_line, end_line, new_text, expected_hash?)`
- `replace_snippet(path, old_text, new_text, expected_occurrences=1, all=false, expected_hash?)`
- `insert_before(path, anchor_text, new_text, expected_occurrences=1, expected_hash?)`
- `insert_after(path, anchor_text, new_text, expected_occurrences=1, expected_hash?)`
- `delete_range(path, start_line, end_line, expected_hash?)`
- `delete_snippet(path, text, expected_occurrences=1, expected_hash?)`
- `append_block(path, new_text, expected_hash?)`
- `prepend_block(path, new_text, expected_hash?)`

Selection rules:
- Use replace_range when exact lines are grounded and local context is stable
- Use replace_snippet when text identity is stronger than line stability
- Use insert_before or insert_after when adding code relative to a stable anchor
- Use delete_range or delete_snippet for explicit removals
- Use append_block or prepend_block only when file semantics support boundary insertion

Before landing:
- verify target context
- detect drift
- confirm occurrence count if anchor-based
- pass `expected_hash` when the caller already has grounded file content and wants explicit drift protection

After landing:
- read back affected region
- verify intended result
- inspect `applied`, `reason`, `diagnostics`, and `preconditions` from the mutation result and report idempotent no-op if already satisfied

<<

[/surgical_text]

---

[structural_code]

>[ref: mutation_rules]
>[ref: safety_contract]

>[ Skill config ]
  intent: "Apply structure-aware mutations to symbols and members when text-local editing is too brittle or too broad."
  input_schema:
    operation: { type: string, required: true, note: "one of replace_symbol, insert_symbol_member, rename_symbol, move_block" }
    path: { type: string, required: true, note: "target file path" }
    target: { type: object, required: true, note: "actual arguments depend on operation: symbol_name/symbol_kind, container_symbol, old_name/new_name, or start_line/end_line plus destination_anchor" }
    content: { type: string, required: false, note: "use new_text or member_text for the owned commands" }
  output_schema:
    applied: { type: boolean, note: "whether the mutation was landed" }
    mutation_type: { type: string, note: "the specific mutation operation used" }
    file_path: { type: string, note: "normalized target path" }
    symbol_scope: { type: object, note: "resolved symbol or structural target" }
    verification: { type: object, note: "post-write verification result" }
    notes: { type: array, note: "symbol resolution, drift, or fallback notes" }
  invariants:
    - Must resolve target structure before mutation
    - Must prefer symbol-aware edits over broad textual rewrites when source structure matters
    - Must not fall back silently to whole-file replacement
    - Must preserve surrounding structure outside the intended target
    - Must fail clearly when target symbol or container cannot be resolved
  allowed_variance:
    structural_locator: true — may use symbol name, kind, container identity, or resolved block boundaries
    member_positioning: true — may insert at start, end, or stable interior position when semantics allow
    fallback_to_text_mode: false — do not silently degrade into surgical text mutation
  failure_modes:
    - Symbol resolution failure
    - Container member insertion point ambiguous
    - Rename scope broader than intended
    - Requested change exceeds safe structural boundary
[/Skill config]<

>>

Use this mode when the change is logically structural, not just textual.

Valid operations include:
- replace_symbol
- insert_symbol_member
- rename_symbol
- move_block

Owned command shapes:
- `replace_symbol(path, symbol_name, symbol_kind, new_text, expected_hash?)`
- `insert_symbol_member(path, container_symbol, member_text, position="end", expected_hash?)`
- `rename_symbol(path, old_name, new_name, scope="file", expected_hash?)`
- `move_block(path, start_line, end_line, destination_anchor, position="after", expected_hash?)`

Selection rules:
- Use replace_symbol for full function, class, method, component, type, or export replacement
- Use insert_symbol_member when adding one method, property, route, field, or case to an existing structure
- Use rename_symbol for token-safe rename within an explicitly bounded scope
- Use move_block for line-grounded repositioning of an existing block relative to a destination anchor; this command is still text-ranged, so require stable line and anchor context

Before landing:
- resolve symbol or structural boundary
- verify uniqueness or bounded ambiguity
- confirm that the requested change fits within structural scope
- for `replace_symbol`, use a concrete `symbol_kind` supported by the repo heuristics such as `function`, `class`, `method`, `constant`, `variable`, `interface`, or `type` when applicable

After landing:
- read back resolved symbol or structure
- verify exact intended structural change
- report failure rather than broadening mutation surface implicitly

<<

[/structural_code]

---

[filesystem]

>[ref: mutation_rules]
>[ref: safety_contract]

>[ Skill config ]
  intent: "Apply explicit file-level mutations to repository topology when extension of existing files is insufficient or inappropriate."
  input_schema:
    operation: { type: string, required: true, note: "one of create_file, delete_file, rename_file, copy_file, fill_template" }
    file_path: { type: string, required: true, note: "target file path or source path" }
    destination_path: { type: string, required: false, note: "destination path for rename or copy operations" }
    content: { type: string, required: false, note: "file content for creation; fill_template uses slots rather than raw content" }
  output_schema:
    applied: { type: boolean, note: "whether the mutation was landed" }
    mutation_type: { type: string, note: "the specific filesystem operation used" }
    affected_paths: { type: array, note: "paths created, removed, renamed, or copied" }
    verification: { type: object, note: "post-write or post-operation verification result" }
    notes: { type: array, note: "policy, collision, or idempotence notes" }
  invariants:
    - Must use explicit file operations only in this mode
    - Must enforce policy restrictions before topology-changing actions
    - Must fail on path collisions unless overwrite is explicitly permitted
    - Must not create new files when extending canonical existing files is sufficient
    - Must verify resulting path state after operation
  allowed_variance:
    overwrite_policy: true — may allow or deny overwrite based on explicit policy
    path_normalization: true — may normalize and validate paths before landing
    silent_scaffolding: false — do not create extra files beyond the requested operation
  failure_modes:
    - Path collision
    - Unauthorized file creation or deletion
    - Duplicate file creation on retry
    - Filesystem mutation used where in-file extension was sufficient
[/Skill config]<

>>

Use this mode only when repository topology must change.

Valid operations include:
- create_file
- delete_file
- rename_file
- copy_file
- fill_template

Owned command shapes:
- `create_file(path, content, overwrite=false)`
- `delete_file(path)`
- `rename_file(old_path, new_path)`
- `copy_file(source_path, destination_path, overwrite=false)`
- `fill_template(path, slots, expected_hash?)`

Selection rules:
- Use create_file only when extraction, new module creation, or missing file introduction is justified
- Use delete_file only for explicit removal of obsolete or invalid artifacts
- Use rename_file when path identity changes but file continuity should remain
- Use copy_file only for controlled templating or snapshot-style duplication
- Use fill_template when an existing file contains explicit `{{slot}}` placeholders and the change is value substitution rather than topology change

Before landing:
- validate path policy
- detect collisions
- detect already-satisfied end state when applicable

After landing:
- verify existence or absence of affected paths
- report exact path transitions
- avoid silent secondary changes

<<

[/filesystem]

---

[coordinated_batch]

>[ref: mutation_rules]
>[ref: safety_contract]

>[ Skill config ]
  intent: "Apply multiple logically linked mutations together with coordinated validation, collision detection, and bounded partial-or-atomic landing behavior."
  input_schema:
    operations: { type: array, required: true, note: "ordered mutation operations spanning one or more mutation surfaces" }
    atomic: { type: boolean, required: false, note: "whether all operations must land together or fail together" }
  output_schema:
    applied: { type: boolean, note: "whether any mutation was landed" }
    atomic: { type: boolean, note: "whether atomic semantics were requested" }
    results: { type: array, note: "per-operation mutation results" }
    verification: { type: object, note: "batch-level verification and consistency result" }
    notes: { type: array, note: "collision, ordering, or partial-landing notes" }
  invariants:
    - Must validate every operation before landing any operation in atomic mode
    - Must detect collisions across operations targeting overlapping regions or paths
    - Must preserve declared operation order unless reordering is explicitly safe and necessary
    - Must report per-operation outcomes even when batch-level failure occurs
    - Must not leave silent inconsistent intermediate states
  allowed_variance:
    atomicity: true — may support atomic or bounded partial landing depending on request
    cross_surface_operations: true — may combine text, structural, and filesystem mutations
    hidden_reordering: false — do not reorder operations without explicit justification
  failure_modes:
    - Intra-batch collision
    - Atomic prevalidation failure
    - Partial landing leaves inconsistent repository state
    - Linked operations applied out of safe order
[/Skill config]<

>>

Use this mode when multiple edits form one logical change.

Typical use cases:
- add import + replace function + update export
- create file + register file + add related test
- rename symbol across bounded files
- update schema + validator + documentation in one coordinated pass

Owned command shapes:
- `batch_mutate(operations=[...], atomic=false)` for one host/tool call that returns per-operation results plus rollback status
- `begin_edit_batch` and `end_edit_batch` for host-managed multi-step editing sessions with deferred verification

Execution rules:
1. Validate all operations first
2. Detect overlapping targets, path collisions, and dependency ordering
3. If atomic true:
   - fail before landing if any operation is invalid
4. If atomic false:
   - land only operations whose preconditions remain valid
   - report blocked operations explicitly
5. Verify the final consistency of the combined state

Owned usage notes:
- In `batch_mutate`, each operation object must use the repo's real argument keys, for example `path`, `file_path`, `old_text`, `new_text`, `anchor_text`, `container_symbol`, `old_name`, `new_name`, `old_path`, `new_path`, `source_path`, or `destination_path` depending on operation type
- `batch_mutate` supports `fill_template` in addition to the text, structural, and filesystem operations above
- `begin_edit_batch` and `end_edit_batch` are coordination actions, not mutation operations inside the `operations` array
- Prefer `begin_edit_batch`/`end_edit_batch` when the agent will issue several separate mutation actions and wants one verification pass at the end; prefer `batch_mutate` when the whole change can be expressed as one explicit operations payload

Return:
- per-operation results
- batch consistency outcome
- notes on collisions, skips, drift, or ordering constraints

<<

[/coordinated_batch]