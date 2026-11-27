---
name: playwright-verification
description: Verify rendered web apps and Skillz artifacts with Playwright using managed Artifact Preview browser-check or Playwright MCP, covering layout bounds, animation frames, keyboard/pointer input, reduced motion, and grounded verification_results.
tags: ["verification", "playwright", "browser", "artifact", "preview", "ui"]
category: verification
priority: 70
modes:
  - managed_preview
  - playwright_mcp
  - measurements_and_motion
---

>[global: verification_rules]
  invariants:
    - Confirm the available browser capability first; never waste turns probing an artifact Docker container for local Playwright/Chromium or running `npx playwright install`.
    - Never fetch host preview URLs (`127.0.0.1:<port>` or `host.docker.internal`) from the artifact container shell; the container loopback is separate from the host browser.
    - Never write temporary `.html` or iframe probe files into `/repo` to exfiltrate DOM data through console errors.
    - Every bound verification check must return concrete, observed evidence; visibility-only smoke steps do not establish pixel alignment, scale invariance, animation easing, or collision clearance.
    - Ground DOM selectors, active/inactive CSS classes, data attributes, and synthetic fixture fields in the rendering source and CSS before asserting; never infer app state solely from third-party library classes (such as React Flow's `inactive` or `.animated`).
    - Isolate multi-check scripts with per-check `try/catch` so every check emits its own `status`, `evidence`, and `next_step`.
    - Use `"fail"` only for reproduced product defects after confirming both the fixture inputs and the DOM/CSS predicate against source; use `"unverified"` with observed classes/values for script, selector, fixture, or unconfirmed DOM-predicate mismatches, and `"blocked"` for unavailable prerequisites (such as an uninstalled host browser).
    - Unverified or failed Playwright verification waits for manual approval to close the issue, so keep reported numbers, selectors, and residual gaps clear and factual.
[/verification_rules]<

---

[managed_preview]

>[ref: verification_rules]

# Managed Artifact Preview (`artifact-preview browser-check`)

In Skillz artifacts, the desktop host runs an isolated, origin-scoped Playwright Chromium inspection browser behind `artifact-preview browser-check`. It works even when Playwright MCP is disabled (`mcp {"operation":"servers"}` returns `[]`).

## 1. Start Preview and inspect before binding (free setup actions)

- Start Preview once first:
  - TreeLoop: `artifact-preview start`
  - Stable: `{"type":"artifact_preview","operation":"start"}`
- Optional exploratory DOM / data inspection (consumes a setup call, **0 check attempts**):
  - Run `artifact-preview browser-check {...}` (with optional `<<< ... >>>`) **without** `verify check-N` when you need to inspect live DOM classes, rendered options, or real local run data before writing assertions. Its `measurements` are surfaced in `latest_browser_measurements`.
  - Keep exploratory probes brief and bind actual assertions with `verify check-N artifact-preview browser-check ...`.

## 2. Exact `browser-check` input schema

All keys are strictly validated. Do not pass unrecognized keys such as `path`, `screenshot`, `action`, or `code`.

Top-level JSON object:
- `route` (required): artifact-relative path starting with `/` (e.g. `"/"`).
- `viewport` (optional): `{"width": 320..2560, "height": 240..1600}` (default `1200×800`).
- `reducedMotion` (optional): `"reduce"` or `"no-preference"`.
- `colorScheme` (optional): `"light"`, `"dark"`, or `"no-preference"`.
- `timeoutMs` (optional): total check timeout in ms (`1000..90000`, default `30000`).
- `steps` (required): `1..24` steps; must include at least one assertion or `evaluate` step. `label` is recommended (optional; defaults from step type/selector if omitted). Per-step `timeoutMs` accepts `100..90000` (clamped to remaining run time).

Supported step types:
- `{"type":"goto","route":"/path","label?":"...","timeoutMs?":10000}`
- `{"type":"click","selector":"...","label?":"...","timeoutMs?":5000}`
- `{"type":"fill","selector":"...","value":"...","label?":"...","timeoutMs?":5000}` (works for text inputs, textareas, number, and range sliders)
- `{"type":"select","selector":"...","value":"...","label?":"...","timeoutMs?":5000}`
- `{"type":"press","key":"Tab|Enter|Escape|...","selector?":"...","label?":"...","timeoutMs?":5000}` (omit `selector` to send key to the currently focused element)
- `{"type":"hover","selector":"...","label?":"...","timeoutMs?":5000}`
- `{"type":"drag","selector":"...","dx":120,"dy":-60,"steps?":8,"label?":"...","timeoutMs?":5000}` (real mouse drag starting at element center)
- `{"type":"wheel","selector":"...","deltaX?":0,"deltaY":-240,"label?":"...","timeoutMs?":5000}` (real mouse wheel over element center)
- `{"type":"wait","ms":400,"label?":"..."}` (`1..60000` ms)
- `{"type":"assert-visible","selector":"...","label?":"...","timeoutMs?":10000}` (pass explicit `timeoutMs` up to `90000` when waiting for long animations/walkthroughs to finish)
- `{"type":"assert-hidden","selector":"...","label?":"...","timeoutMs?":10000}`
- `{"type":"assert-text","selector":"...","text":"...","exact?":false,"label?":"...","timeoutMs?":10000}`
- `{"type":"assert-focused","selector":"...","label?":"...","timeoutMs?":10000}`
- `{"type":"evaluate","script":"async () => ({ ... })","label?":"...","timeoutMs?":30000}` (runs inside the isolated preview page; defaults to `max(15000, runTimeoutMs - 2000)`; return compact JSON <= 16 KB)

## 2a. Selector Hygiene, Source-Grounded Predicates & Long Animations

- **Avoid strict-mode collisions**: Playwright `locator()` throws on multiple matches. Never use CSS `:first-of-type` across sibling containers (e.g. two `<label><input type="range" /></label>` controls both match `input[type=range]:first-of-type`). Prefer unique Playwright selectors such as `label:has-text('Speed') input[type=range]`, `role=slider[name="Speed"]`, or explicit `>> nth=0`.
- **Verify active/inactive classes and fixture contracts in source**: Before asserting on element classes or constructing a mocked `fetch` fixture, read the rendering component, its CSS rules, and any matching/correlation helper. If a keyword search (e.g. `animated:`) returns 0 matches, read the actual element/edge styling block and CSS file instead of guessing. Third-party libraries (such as React Flow) may keep classes like `inactive` on non-focusable edges even when the app applies its own active class (e.g. `architecture-active`) — assert the app's class, `aria-*`/`data-*` attribute, or `getComputedStyle(...)`.
- **Do not emit `"fail"` on an unconfirmed DOM predicate**: If a probe returns an unexpected class list or synthetic fixture miss (for example, an app active class appears alongside a library `inactive` class), emit `"unverified"` with the observed classes/values, confirm the rule in source, and rerun.
- **Long walkthroughs / animations**: set top-level `timeoutMs` up to `90000` (e.g. `60000` for a ~45s 1× walkthrough) and set matching `timeoutMs` on `evaluate` or `assert-visible` (e.g. waiting for `[data-walkthrough='finished']`) instead of chaining artificial short sleeps.
- **`browser_observations` format**: when returning `browser_observations` inside `verification_results`, use `{ "type": "...", "label": "..." }` objects (or omit it to keep the host's recorded step observations).

## 3. Literal JS blocks in TreeLoop (`<<< ... >>>`)

To avoid JSON string-escaping errors in TreeLoop, omit `"script"` from a single `evaluate` step in the JSON header and put raw JavaScript between `<<<` and `>>>`:

```text
verify check-2,check-3 artifact-preview browser-check {"route":"/","viewport":{"width":1200,"height":800},"timeoutMs":30000,"steps":[{"type":"click","selector":"button:has-text('Architecture')","label":"Open Architecture"},{"type":"assert-visible","selector":".architecture-canvas .react-flow","label":"Overview canvas visible"},{"type":"evaluate","label":"Measure centering and zoom"}]} <<<
async () => {
  const results = {};
  try {
    // Perform DOM measurements and return compact verdicts
    results["check-2"] = { status: "pass", evidence: "Delta x=0.1px, y=0.0px (<= 1px)." };
  } catch (err) {
    results["check-2"] = { status: "unverified", evidence: String(err), next_step: "Verify selectors." };
  }
  return { verification_results: results };
}
>>>
```

In Stable mode, pass the same object under `{"type":"verify","checks":["check-2"],"action":{"type":"artifact_preview","operation":"browser-check","input":{...}}}` with `"script"` inline.

## 4. When `browser-check` returns `unavailable: true`

That means the optional Playwright inspection browser has not been downloaded on the desktop host yet. Do not retry in a loop or try to install Chromium inside Docker; report the check as `blocked` with next step: `"Install the Playwright inspection browser in artifact Setup, or enable Playwright MCP, then rerun the check."`

[/managed_preview]

---

[playwright_mcp]

>[ref: verification_rules]

# Playwright MCP Verification

Use Playwright MCP when `playwright` is listed by `mcp {"operation":"servers"}` (default for workspace repos, opt-in for artifacts).

1. **Setup (consumes no check attempts)**:
   - Start Preview if in an artifact (`artifact-preview start`) and use its published `http://127.0.0.1:<port>` URL.
   - Navigate with `mcp {"operation":"call","server":"playwright","name":"browser_navigate","arguments":{"url":"http://127.0.0.1:<port>/"}}`.
   - Inspect structure inline with `browser_snapshot` (omit `filename`).
   - Prepare disposable fixtures with unbound `browser_evaluate`, checking HTTP status and `Content-Type` before parsing JSON.
2. **Real input**:
   - Use `browser_click`, `browser_drag`, `browser_press_key`, and `browser_select_option` with refs or verified selectors from the latest snapshot.
   - Verify preconditions after input before clicking dependent actions.
3. **Bound assertions**:
   - Use `verify check-N mcp {"operation":"call","server":"playwright","name":"browser_evaluate"} <<< ... >>>`.
   - Return ONLY `{ verification_results: { "check-N": { status, evidence, next_step?, browser_observations? } } }`.
   - Keep returned JSON compact; inspect large DOM trees in a separate unbound call.

[/playwright_mcp]

---

[measurements_and_motion]

>[ref: verification_rules]

# Deterministic Patterns for Layout, Zoom, Motion, Focus & Drag

Use these patterns inside `artifact-preview browser-check` (`evaluate` + input steps) or Playwright MCP (`browser_evaluate`):

## 1. Center alignment, bounds, overlap, and clearance

- Measure rendered geometry with `getBoundingClientRect()` on the actual surface (e.g. `.react-flow`) and target elements (cards, labels, controls).
- For centering checks:
  - `dx = (card.left + card.width / 2) - (canvas.left + canvas.width / 2)`
  - `dy = (card.top + card.height / 2) - (canvas.top + canvas.height / 2)`
  - Assert `Math.abs(dx) <= tolerance && Math.abs(dy) <= tolerance` and report exact `dx`/`dy` in `evidence`.
- For label clearance / collision checks:
  - Collect all label rects and card/control rects.
  - Compute exact minimum pairwise separation (`max(0, max(a.left - b.right, b.left - a.right, a.top - b.bottom, b.top - a.bottom))` or signed overlap) and verify every label fits inside canvas bounds.

## 2. Viewport scale invariance and eased animation frames

- Parse transform scale cleanly via `new DOMMatrix(getComputedStyle(viewportEl).transform).a` (and translation `e`, `f`).
- Sample consecutive `requestAnimationFrame` timestamps starting immediately before triggering Play / the transition through settle (`~400-800ms` or until `|dx| < 0.05 && |dy| < 0.05` across consecutive frames):
  - **Constant zoom**: `maxScaleDrift = Math.max(...samples.map(s => Math.abs(s.scale - startScale)))` — verify `maxScaleDrift <= 1e-3` at two distinct starting zooms (change zoom between runs using `wheel` or zoom controls, and verify `Math.abs(zoom2 - zoom1) >= 0.1`).
  - **Eased motion**: verify intermediate frames move monotonically toward the target with non-zero intermediate steps (not a 1-frame jump) and settle within tolerance.

## 3. Rapid transitions, paused manual drag, and reduced motion

- **Rapid hits**: use `fill` on the speed slider (e.g. `"3"`) or click controls rapidly, wait for the final pan to settle, and assert the latest active node center matches canvas center.
- **Paused manual panning**: pause playback, perform a real `drag` step on the canvas (`dx: 140, dy: -80`), then run an `evaluate` step that records the viewport transform immediately and again after `400ms`, verifying no camera-follow override occurred (`|e2 - e1| < 0.5 && |f2 - f1| < 0.5`) and the drag actually moved the canvas.
- **Reduced motion**: run a `browser-check` with `"reducedMotion": "reduce"` in the top-level JSON, start playback, and sample the first rendered active frame to confirm immediate centering at the starting zoom without multi-frame lag.
- **Keyboard navigation & focus**: use `press` steps (`Tab`, `Enter`, `Escape`) paired with `assert-focused` on the expected control or heading rather than fragile CSS selectors alone.

[/measurements_and_motion]
