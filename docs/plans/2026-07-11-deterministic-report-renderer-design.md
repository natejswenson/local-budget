# Deterministic visual-report renderer (2026-07-11)

**Supersedes** the "pure instructions, no shipped code artifacts" decision in
`2026-07-05-visual-artifact-reports-design.md`, and the recipe evolutions in
`2026-07-06-visual-report-fixes-design.md` and
`2026-07-06-subscriptions-and-combined-chart-design.md`. Those docs remain the
rationale record for the recipe *rules*, which are preserved verbatim — the
change is only WHERE they execute.

## What changed and why

The visual report was LLM-authored HTML from ~290 lines of prose recipes in
`budget-visualizer/SKILL.md`, rendered via a hardcoded macOS Chrome path.
Observed costs by 2026-07-11:

- **Untestable.** Every edge case (floor carve-outs, shared bar scale,
  recurring cross-reference) depended on the model re-reading dense prose per
  render; drift was silent and had already required two fix-design docs.
- **Palette divergence.** Dashboard and PDF each hardcoded their own colors —
  three color languages for the same month.
- **Rules duplicated in markdown.** The bill-like allowlist and merchant
  aliases were retyped in prose, parallel to real code.
- **Brittle + leaky.** One moved Chrome binary broke the whole path; PDFs
  landed 0644 in a 0755 dir (outside the app's 0600 discipline); scratch HTML
  with a month of financials had no defined lifecycle.

## The renderer

`src/local_budget/report/`: `palette.py` (parses the shared
`web/static/palette.css`, also linked by the dashboard), `flags.py` (allowlist,
aliases, month-filter/cross-reference rules as pure functions), `charts.py`
(stat row / spend-vs-budget / flags / new trend chart → HTML fragments),
`html.py` (page assembly, `@page`, escaping), `pdf.py` (Chrome discovery with
`LOCAL_BUDGET_CHROME` override, `--force-device-scale-factor=2`, 0600 output,
scratch cleanup in `finally`), `render.py` (orchestrator). Output goes to
`paths.reports_dir()` (0700, same `reports/` location as before).

Exposed as the `render_report(period, narrative?)` MCP tool (save_brief-style
period validation + path confinement; in the skill-lint write-gate set) and
`budget report-pdf <period>` CLI. The LLM's only contribution is the optional
escaped `narrative` paragraph.

Tests: golden-HTML snapshots per recipe (`tests/golden/report/`,
`UPDATE_GOLDEN=1` to regenerate), table-driven flags rules, mocked-subprocess
pdf tests + one skipif-Chrome real render, palette drift guard.

The original prose recipe survives, condensed, as the fallback appendix in
`budget-visualizer/SKILL.md` — used only when `render_report` reports Chrome
missing.
