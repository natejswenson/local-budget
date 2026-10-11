import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import webbrowser

from click.testing import CliRunner
import pytest

from local_budget import db, inbox_adapter
from local_budget.connectors.plaid import cli, locks, store, sync
from local_budget.connectors.plaid.credentials import CredentialError
from .test_apply import enabled, health_response
from .test_integration_cli import mock_provider, page, transaction

ROOT = Path(__file__).resolve().parents[2]
ARGS = ["sync", "--all", "--non-interactive", "--json"]


def test_schedule_template_is_opt_in_and_six_hourly():
    payload = plistlib.loads(
        (ROOT / "ops/com.local-budget.plaid-sync.plist.template").read_bytes()
    )
    assert payload["StartInterval"] == 21600
    assert payload["RunAtLoad"] is True
    assert "KeepAlive" not in payload
    assert "EnvironmentVariables" not in payload


def executable(path, text):
    path.write_text("#!/bin/bash\nset -eu\n" + text)
    path.chmod(0o700)


def test_wrapper_executes_pinned_cli_and_private_output(tmp_path):
    binary = tmp_path / "budget cli"
    data = tmp_path / "data & <bank>"
    output = tmp_path / "output"
    executable(binary, 'printf "%s\\n" "$LOCAL_BUDGET_DATA_DIR" "$@" > "$OUTPUT"\n')
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "ops/budget-plaid-sync.sh"), str(binary), str(data)],
        env={**os.environ, "OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == [str(data), "plaid", *ARGS]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    wrapper = (ROOT / "ops/budget-plaid-sync.sh").read_text()
    assert "uv " not in wrapper and "source " not in wrapper and ".bash" not in wrapper
    assert (
        subprocess.run(
            ["/bin/bash", str(ROOT / "ops/budget-plaid-sync.sh")], capture_output=True
        ).returncode
        == 64
    )


def install_fixture(tmp_path):
    repo = tmp_path / 'repo & <literal> "space"'
    venv = tmp_path / "venv space"
    data = tmp_path / "data & <literal>"
    (repo / "ops").mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    for name in ["budget-plaid-sync.sh", "com.local-budget.plaid-sync.plist.template"]:
        shutil.copy(ROOT / "ops" / name, repo / "ops" / name)
    executable(venv / "bin/budget", "exit 0\n")
    commands = tmp_path / "commands"
    commands.mkdir()
    calls = tmp_path / "calls"
    executable(commands / "launchctl", 'printf "%s\\n" "$@" >> "$CALLS"\n')
    executable(commands / "plutil", 'printf "%s\\n" "$@" >> "$CALLS"\n')
    env = {**os.environ, "PATH": f"{commands}:/usr/bin:/bin", "CALLS": str(calls)}
    return repo, venv, data, calls, env


def test_installer_literal_paths_private_logs_and_lint_before_bootstrap(tmp_path):
    repo, venv, data, calls, env = install_fixture(tmp_path)
    result = subprocess.run(
        [
            "/bin/bash",
            str(ROOT / "ops/install-plaid-sync.sh"),
            str(repo),
            str(venv),
            str(data),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    plist = data / "plaid-schedule/com.local-budget.plaid-sync.plist"
    payload = plistlib.loads(plist.read_bytes())
    assert payload["ProgramArguments"] == [
        "/bin/bash",
        str(repo / "ops/budget-plaid-sync.sh"),
        str(venv / "bin/budget"),
        str(data),
    ]
    assert payload["StandardOutPath"] == str(data / "plaid-schedule/logs/sync.log")
    assert payload["StandardErrorPath"] == str(
        data / "plaid-schedule/logs/sync.err.log"
    )
    assert calls.read_text().splitlines() == [
        "-lint",
        str(plist),
        "bootout",
        f"gui/{os.getuid()}/com.local-budget.plaid-sync",
        "bootstrap",
        f"gui/{os.getuid()}",
        str(plist),
    ]
    for path in [data, plist.parent, plist.parent / "logs"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in [
        plist,
        Path(payload["StandardOutPath"]),
        Path(payload["StandardErrorPath"]),
    ]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    if shutil.which("plutil") and os.uname().sysname == "Darwin":
        assert (
            subprocess.run(
                ["/usr/bin/plutil", "-lint", str(plist)], capture_output=True
            ).returncode
            == 0
        )


def test_installer_rejects_relative_paths_without_launchctl(tmp_path):
    repo, venv, data, calls, env = install_fixture(tmp_path)
    result = subprocess.run(
        [
            "/bin/bash",
            str(ROOT / "ops/install-plaid-sync.sh"),
            "relative",
            str(venv),
            str(data),
        ],
        env=env,
        capture_output=True,
    )
    assert result.returncode != 0
    assert not calls.exists() and not data.exists()


def prohibit_interaction(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Scheduled sync must not prompt or open a browser")

    monkeypatch.setattr(cli.click, "prompt", forbidden)
    monkeypatch.setattr(cli.click, "confirm", forbidden)
    monkeypatch.setattr(webbrowser, "open", forbidden)


def test_bad_item_continues_and_locked_keychain_is_safe(
    plaid_db, seed_item, monkeypatch
):
    first, second = seed_item("bad"), seed_item()
    enabled(plaid_db, first)
    with db.connect(plaid_db) as conn:
        conn.execute(
            "INSERT INTO accounts(account_id,institution,acct_type,acct_last4,acct_hash) VALUES(2,'TEST','checking','9999','test-2')"
        )
        conn.execute(
            "UPDATE plaid_accounts SET local_account_id=2 WHERE item_id=?", (first,)
        )
    enabled(plaid_db, second)
    mock_provider(
        monkeypatch,
        [
            health_response("/item/get"),
            health_response("/accounts/get"),
            page([transaction()]),
        ],
    )
    original = cli._configured
    attempts = []

    def configured(env, **kwargs):
        assert kwargs["interactive"] is False
        attempts.append(env)
        if len(attempts) == 1:
            raise CredentialError("credential_locked")
        return original(env, **kwargs)

    monkeypatch.setattr(cli, "_configured", configured)
    prohibit_interaction(monkeypatch)
    result = CliRunner().invoke(cli.plaid, ARGS)
    assert result.exit_code == 1, result.output
    rows = json.loads(result.output)
    assert rows[0] == {
        "connection": first,
        "outcome": "failed",
        "safe_error_code": "credential_locked",
    }
    assert rows[1]["connection"] == second and rows[1]["inserted"] == 1
    assert len(attempts) == 2
    assert all(
        value not in result.output
        for value in ["FABRICATED", "SHOP", "a1", "1200", "2026-10-01"]
    )


@pytest.mark.parametrize("kind", ["item", "intake"])
def test_lock_contention_skips_safely(plaid_db, seed_item, monkeypatch, kind):
    item = seed_item()
    enabled(plaid_db, item)
    requests = mock_provider(
        monkeypatch,
        []
        if kind == "item"
        else [health_response("/item/get"), health_response("/accounts/get"), page()],
    )
    prohibit_interaction(monkeypatch)
    with (
        locks.item_locks(plaid_db, [item])
        if kind == "item"
        else inbox_adapter.intake_lock()
    ) as acquired:
        assert acquired
        result = CliRunner().invoke(cli.plaid, ARGS)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0] == {
        "connection": item,
        "outcome": "skipped",
        "safe_error_code": f"{kind}_busy",
    }
    if kind == "item":
        assert requests == []


@pytest.mark.parametrize(
    "code,updates,size,deadline",
    [
        ("sync_deadline", 0, 0, 0),
        ("sync_limit", 50001, 0, 300),
        ("sync_limit", 0, 64 * 1024 * 1024 + 1, 300),
    ],
)
def test_fetch_failure_preserves_cursor_and_facts(
    plaid_db, seed_item, monkeypatch, code, updates, size, deadline
):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute("UPDATE plaid_items SET cursor='saved'")
    mock_provider(
        monkeypatch, [health_response("/item/get"), health_response("/accounts/get")]
    )

    def failed(*args, **kwargs):
        sync.check_fetch_limits(
            updates=updates, response_bytes=size, deadline=deadline, now=0
        )

    monkeypatch.setattr(sync, "fetch_batch", failed)
    prohibit_interaction(monkeypatch)
    result = CliRunner().invoke(cli.plaid, ARGS)
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)[0]["safe_error_code"] == code
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)["cursor"] == "saved"
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
