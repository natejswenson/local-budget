"""Deterministic visual-report renderer (design 2026-07-11).

Report data → HTML/inline-SVG → PDF, in tested Python instead of per-request
LLM-authored markup. Supersedes the prose-recipe path in
.claude/skills/budget-visualizer/SKILL.md (kept there as a documented
fallback); the LLM contributes only an optional narrative paragraph.
"""
