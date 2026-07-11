"""pdf.py + paths.reports_dir() — everything mocked except one skipif-Chrome
integration render (CI has no Chrome)."""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from local_budget import paths
from local_budget.report import pdf


@pytest.fixture(autouse=True)
def no_network_egress():
    """Override the conftest socket-block: the tool tests drive async handlers
    via asyncio (which needs the self-pipe socket) but perform NO network I/O."""
    yield


# ── reports_dir hardening (siege S3) ─────────────────────────────────────────
def test_reports_dir_override_and_0700(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_REPORTS_DIR", str(tmp_path / "r"))
    d = paths.reports_dir()
    assert d == tmp_path / "r" and d.is_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


# ── chrome discovery ─────────────────────────────────────────────────────────
def test_chrome_env_override_wins(tmp_path, monkeypatch):
    fake = tmp_path / "chrome"
    fake.touch()
    monkeypatch.setenv("LOCAL_BUDGET_CHROME", str(fake))
    assert pdf.chrome_path() == str(fake)


def test_chrome_env_override_missing_is_actionable(monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_CHROME", "/nope/chrome")
    with pytest.raises(pdf.ChromeNotFoundError, match="does not exist"):
        pdf.chrome_path()


def test_chrome_not_found_names_the_fix(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCAL_BUDGET_CHROME", raising=False)
    monkeypatch.setattr(pdf, "_MAC_APPS", ())
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(pdf.ChromeNotFoundError, match="LOCAL_BUDGET_CHROME"):
        pdf.chrome_path()


# ── render_pdf (subprocess mocked) ───────────────────────────────────────────
@pytest.fixture
def fake_chrome(tmp_path, monkeypatch):
    fake = tmp_path / "chrome"
    fake.touch()
    monkeypatch.setenv("LOCAL_BUDGET_CHROME", str(fake))
    calls = []

    def _run(argv, **kwargs):
        calls.append((argv, kwargs))
        # emulate Chrome writing the PDF target
        out = next(a for a in argv if a.startswith("--print-to-pdf=")).split("=", 1)[1]
        Path(out).write_bytes(b"%PDF-1.7 fake")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", _run)
    return calls


def test_render_pdf_argv_perms_and_scratch_cleanup(tmp_path, fake_chrome):
    out = tmp_path / "reports" / "budget-report-2026-06.pdf"
    result = pdf.render_pdf("<html>x</html>", out)
    assert result == out and out.read_bytes().startswith(b"%PDF")
    argv, kwargs = fake_chrome[0]
    assert "--headless" in argv and "--force-device-scale-factor=2" in argv
    assert "--no-pdf-header-footer" in argv and kwargs["check"] is True
    # 0600 output, no scratch html left behind
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert not list(out.parent.glob("*.html")) and not list(out.parent.glob(".*.html"))


def test_render_pdf_cleans_scratch_on_failure(tmp_path, monkeypatch):
    fake = tmp_path / "chrome"
    fake.touch()
    monkeypatch.setenv("LOCAL_BUDGET_CHROME", str(fake))

    def _boom(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr=b"render failed")

    monkeypatch.setattr(subprocess, "run", _boom)
    out = tmp_path / "r" / "x.pdf"
    with pytest.raises(subprocess.CalledProcessError):
        pdf.render_pdf("<html>x</html>", out)
    # the scratch html (full financials) never survives a failed render
    assert not list(out.parent.glob("*.html")) and not list(out.parent.glob(".*.html"))


# ── one real render, skipped when no Chrome is installed ─────────────────────
def _real_chrome() -> bool:
    try:
        pdf.chrome_path()
        return True
    except pdf.ChromeNotFoundError:
        return False


@pytest.mark.skipif(not _real_chrome(), reason="no Chrome/Chromium installed")
def test_render_pdf_integration(tmp_path):
    out = tmp_path / "real.pdf"
    pdf.render_pdf("<!doctype html><html><body><h1>ok</h1></body></html>", out)
    blob = out.read_bytes()
    assert blob[:5] == b"%PDF-" and len(blob) > 1000
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


# ── the render_report MCP tool + report-pdf CLI (mocked render) ──────────────
def test_render_report_tool_validates_and_reports_path(data_dir, monkeypatch, tmp_path):
    import asyncio

    from local_budget import db
    from local_budget.agent import tools
    from local_budget.report import render as report_render

    db.init_schema()

    def _call(args):
        return asyncio.run(tools.SPEC_BY_NAME["render_report"].handler(args))

    assert "invalid period" in _call({"period": "junk"})["error"]
    assert "invalid period" in _call({"period": "../etc"})["error"]

    monkeypatch.setattr(report_render, "render_pdf",
                        lambda page, out, **kw: out.write_bytes(b"%PDF") or out)
    monkeypatch.setenv("LOCAL_BUDGET_REPORTS_DIR", str(tmp_path / "reports"))
    res = _call({"period": "2026-06", "narrative": "steady month"})
    assert res.get("ok"), res
    assert res["path"].endswith("budget-report-2026-06.pdf")
    assert "✓ visual report saved" in res["rendered"]


def test_render_report_tool_chrome_missing_names_fallback(data_dir, monkeypatch):
    import asyncio

    from local_budget import db
    from local_budget.agent import tools
    from local_budget.report import render as report_render

    db.init_schema()

    def _no_chrome(page, out, **kw):
        raise pdf.ChromeNotFoundError("no Chrome/Chromium found")

    monkeypatch.setattr(report_render, "render_pdf", _no_chrome)
    res = asyncio.run(tools.SPEC_BY_NAME["render_report"].handler({"period": "2026-06"}))
    assert "Fallback" in res["error"] and "budget-visualizer" in res["error"]
