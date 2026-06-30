"""Make the eval modules importable as bare top-level modules.

`tests/evals/` (harness, specs) and `scripts/` (the `eval` runner) are put on
`sys.path` so the eval tests and the runner can `import harness` / `import specs`
without a package prefix — and without touching pyproject's pytest config.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tests" / "evals", _ROOT / "scripts"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)
