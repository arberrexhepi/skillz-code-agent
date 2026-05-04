---
name: codebase-diagnostics
description: Deterministic diagnostics layer for detecting and normalizing codebase problems by error surface prior to mutation, retry, or completion.
modes:
  - syntax_and_structure
  - semantics_and_types
  - integration_and_runtime
  - governance_and_risk
  - aggregate
---

>[global: diagnostics_rules]
  invariants:
    - Always prefer tool-derived diagnostics over speculative reasoning
    - Never fabricate errors; only report verifiable findings
    - Deduplicate overlapping diagnostics across tools
    - Separate blocking errors from non-blocking warnings
    - Normalize all findings into a shared diagnostics structure
  failure_modes:
    - Reporting speculative or unverifiable issues
    - Duplicate or conflicting diagnostics
[/diagnostics_rules]<

>[global: output_contract]
  invariants:
    - Every diagnostic must include file path, message, category, and severity
    - Categories must be explicit and stable
    - Summary must accurately reflect counts by severity and category
    - Notes must declare scope limitations when present
  failure_modes:
    - Missing required diagnostic fields
    - Inconsistent categorization
    - Inaccurate summary counts
[/output_contract]<

---

[syntax_and_structure]

>[ref: diagnostics_rules]
>[ref: output_contract]

>[ Skill config ]
  intent: "Detect parse, syntax, formatting, and structural configuration problems in source and machine-readable project files."
  input_schema:
    paths: { type: array, required: false, note: "target files or directories; defaults to changed files when omitted" }
  output_schema:
    diagnostics: { type: array, note: "normalized syntax, format, and config diagnostics" }
    summary: { type: object, note: "counts by severity and category" }
    notes: { type: array, note: "scope and limitation notes" }
  invariants:
    - Must detect syntax and parse failures before deeper diagnostics
    - Must include structural config validation when relevant
    - Must classify findings only as syntax, format, or config in this mode
    - Must prefer changed files when explicit paths are absent
  allowed_variance:
    scope: true — may operate on changed files or explicit targets
    formatter_strictness: true — may follow repository formatting policy when available
    deeper_semantics: false — do not include type or runtime findings in this mode
  failure_modes:
    - Parse failures not surfaced
    - Invalid config files missed
    - Structural issues mixed with semantic categories
[/Skill config]<

>>

Use this mode first when the failure surface is likely local and syntactic.

Run only diagnostics that answer:
- does this parse
- is the structure valid
- is the config readable and well-formed

Typical checks:
- syntax_check(paths=[...])
- config_validate(paths=[...])

Owned tool notes:
- There is no standalone `format_check` command in this repo.
- If formatting failures are enforced through another tool in a specific project, that belongs to a repo-specific follow-up, not this base contract.
- When `paths` is omitted, prefer `changed_files_check()` only if you need the host's changed-file expansion; otherwise pass explicit `paths`.

Return only syntax, format, and config findings.

<<

[/syntax_and_structure]

---

[semantics_and_types]

>[ref: diagnostics_rules]
>[ref: output_contract]

>[ Skill config ]
  intent: "Detect semantic correctness issues including type failures, invalid symbol usage, broken imports, and high-signal lint problems."
  input_schema:
    paths: { type: array, required: false, note: "target files or directories; defaults to changed files plus related files when omitted" }
  output_schema:
    diagnostics: { type: array, note: "normalized type, semantic, dependency, and lint diagnostics" }
    summary: { type: object, note: "counts by severity and category" }
    notes: { type: array, note: "scope and limitation notes" }
  invariants:
    - Must include type diagnostics when the language supports them
    - Must include unresolved symbol and import/dependency issues when detectable
    - Must prefer correctness-oriented lint findings over cosmetic-only findings
    - Must classify findings only as type, lint, or dependency in this mode
  allowed_variance:
    scope: true — may include related files beyond explicit targets
    lint_density: true — may suppress low-value stylistic findings
    build_execution: false — do not include build or runtime findings in this mode
  failure_modes:
    - Type failures missed
    - Broken imports unresolved
    - Lint spam obscuring material issues
[/Skill config]<

>>

Use this mode when the code parses but may still be wrong.

Run diagnostics that answer:
- do symbols resolve
- do types line up
- are imports and dependencies wired correctly
- are there high-signal correctness warnings

Typical checks:
- type_check(paths=[...], scope="changed"|"project")
- lint_check(paths=[...], scope="changed"|"project")
- dependency_check(paths=[...])

Owned tool notes:
- Use explicit `paths` when you already know the candidate surface.
- Use `scope="changed"` for narrow validation and `scope="project"` when related files must be included.

Return only type, lint, and dependency findings.

<<

[/semantics_and_types]

---

[integration_and_runtime]

>[ref: diagnostics_rules]
>[ref: output_contract]

>[ Skill config ]
  intent: "Detect integration, build, test, and startup/runtime failures caused by cross-file coupling or environment-dependent execution."
  input_schema:
    paths: { type: array, required: false, note: "target files or directories; defaults to changed files plus discovered impact surface when omitted" }
  output_schema:
    diagnostics: { type: array, note: "normalized build, test, and runtime diagnostics" }
    summary: { type: object, note: "counts by severity and category" }
    notes: { type: array, note: "scope and environment notes" }
  invariants:
    - Must include build diagnostics when the project has a build surface
    - Must include targeted test diagnostics when applicable
    - Must surface runtime startup failures when reproducible
    - Must classify findings only as build, test, or runtime in this mode
  allowed_variance:
    test_scope: true — may run related tests instead of full suite
    runtime_depth: true — may stop at smoke-check level if full execution is expensive
    policy_checks: false — do not include governance findings in this mode
  failure_modes:
    - Build regression not surfaced
    - Relevant tests skipped
    - Runtime startup failure missed
[/Skill config]<

>>

Use this mode when the failure surface is cross-file or execution-dependent.

Run diagnostics that answer:
- does the project build
- do related tests pass
- does the app or service start cleanly
- do integration surfaces fail under execution

Typical checks:
- build_check(targets=[...])
- test_check(targets=[...], mode="related"|"full"|"deep")
- runtime_smoke_check(target="<finite command>")

Owned tool notes:
- `test_check` defaults to related scope; prefer that before `mode="full"` or `mode="deep"`.
- `runtime_smoke_check` requires an explicit finite startup or probe command and must not be used for watch/dev servers.
- If you need a normalized combined surface instead of raw per-check orchestration, escalate to `project_problems(mode="standard"|"deep")`.

Return only build, test, and runtime findings.

<<

[/integration_and_runtime]

---

[governance_and_risk]

>[ref: diagnostics_rules]
>[ref: output_contract]

>[ Skill config ]
  intent: "Detect repository policy violations, security risks, duplication hazards, and maintainability faults that threaten architectural integrity."
  input_schema:
    paths: { type: array, required: false, note: "target files or directories; defaults to changed files with project-wide expansion when necessary" }
  output_schema:
    diagnostics: { type: array, note: "normalized policy, security, duplication, and dead-code diagnostics" }
    summary: { type: object, note: "counts by severity and category" }
    notes: { type: array, note: "scope and confidence notes" }
  invariants:
    - Must treat policy violations as first-class findings
    - Must surface security findings only when evidence is concrete or high-signal
    - Must distinguish maintainability hazards from active runtime faults
    - Must classify findings only as policy, security, dead_code, or duplication in this mode
  allowed_variance:
    project_scope: true — may expand beyond changed files when local evidence implies broader risk
    dead_code_confidence: true — may suppress weak dead-code guesses
    execution_checks: false — do not include build or runtime findings in this mode
  failure_modes:
    - Security issues reported with weak evidence
    - Policy violations missed
    - Duplication or dead-code noise overwhelms actionable findings
[/Skill config]<

>>

Use this mode when architectural safety matters more than immediate execution state.

Run diagnostics that answer:
- does this violate repository policy
- does this introduce security risk
- is this duplicative or dead
- does this conflict with engineering constraints

Typical checks:
- policy_check(paths=[...])
- security_check(paths=[...])
- dead_code_check(paths=[...], scope="project")
- duplication_check(paths=[...], threshold=30)

Owned tool notes:
- `dead_code_check` expects explicit paths plus a scope.
- `duplication_check` requires a concrete path set and an integer threshold.

Return only policy, security, dead_code, and duplication findings.

<<

[/governance_and_risk]

---

[aggregate]

>[ref: diagnostics_rules]
>[ref: output_contract]

>[ Skill config ]
  intent: "Run multiple diagnostic surfaces together and return a deduplicated, normalized project problems view suitable for final decision-making."
  input_schema:
    paths: { type: array, required: false, note: "target files or directories; defaults to changed files plus discovered impact surface when omitted" }
    surfaces: { type: array, required: false, note: "subset of diagnostic surfaces to run; defaults to all" }
  output_schema:
    diagnostics: { type: array, note: "fully normalized, deduplicated diagnostics across requested surfaces" }
    summary: { type: object, note: "counts by severity and category" }
    notes: { type: array, note: "scope, skipped surfaces, and confidence notes" }
  invariants:
    - Must deduplicate overlapping findings across surfaces
    - Must preserve category identity for every diagnostic
    - Must separate blocking from non-blocking findings
    - Must declare skipped or unavailable surfaces explicitly
  allowed_variance:
    surface_selection: true — may run only requested or relevant surfaces
    execution_depth: true — may use targeted scope before project-wide scope
    fabrication: false — do not infer diagnostics not produced by tools
  failure_modes:
    - Overlapping findings not merged
    - Missing declaration of skipped surfaces
    - Aggregate output obscures blocking issues
[/Skill config]<

>>

Use this mode when the agent needs a unified problems view.

1. Select surfaces:
   - syntax_and_structure
   - semantics_and_types
   - integration_and_runtime
   - governance_and_risk

2. Run requested surfaces in a sensible order:
   - syntax_and_structure first
   - semantics_and_types second
   - integration_and_runtime third
   - governance_and_risk as needed

3. Prefer owned aggregate commands when they already match the desired scope:
  - use `changed_files_check()` for a changed-file aggregate surface
  - use `project_problems(mode="fast"|"standard"|"deep")` for a normalized project view
  - only orchestrate the individual checks yourself when you need a selective subset the aggregate commands do not expose

4. Normalize and deduplicate:
   - merge equivalent findings
   - preserve strongest severity
   - retain original category

5. Return:
   - unified diagnostics list
   - summary by severity and category
   - notes on scope, skips, and ambiguity

<<

[/aggregate]